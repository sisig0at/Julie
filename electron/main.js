const { app, BrowserWindow, ipcMain, globalShortcut } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');
const crypto = require('crypto');
const { autoUpdater } = require('electron-updater');

// When launched from a file manager there is no terminal: stdout is a closed
// pipe and any console.write throws EIO, which would crash the main process.
for (const level of ['log', 'warn', 'error']) {
  const orig = console[level].bind(console);
  console[level] = (...args) => {
    try {
      orig(...args);
    } catch (e) {
      /* ignore EIO: no console available */
    }
  };
}

const DEV_BACKEND_PORT = 8765;
let mainWindow = null;
let backendProcess = null;
let backendUrl = null;
let apiToken = '';   // local API bearer token (see ensureApiToken)

// Same location src/config.py uses - the config lives OUTSIDE the repo
// (%APPDATA%\Jarvis\config.json on Windows, ~/.config/jarvis/config.json else).
function configFilePath() {
  if (process.platform === 'win32') {
    return path.join(process.env.APPDATA || os.homedir(), 'Jarvis', 'config.json');
  }
  const base = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config');
  return path.join(base, 'jarvis', 'config.json');
}

// First start creates the API token once and stores it in config.json. It is
// handed to the backend via JARVIS_API_TOKEN (spawn) and to the renderer via
// IPC ('get-api-token') - deliberately never sent over HTTP.
function ensureApiToken() {
  const file = configFilePath();
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    let data = {};
    try {
      const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) data = parsed;
    } catch (e) {
      console.warn('Config unreadable, starting a fresh one:', e.message);
    }
    if (typeof data.api_token !== 'string' || !data.api_token) {
      data.api_token = crypto.randomBytes(32).toString('hex');
      fs.writeFileSync(file, JSON.stringify(data, null, 2), 'utf8');
      console.log('Generated API token in', file);
    }
    return data.api_token;
  } catch (e) {
    console.warn('Could not prepare API token:', e.message);
    return '';
  }
}

// Only one instance allowed: launching the app again (double click, updater)
// focuses the existing window instead of spawning a second backend.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
}

function resolveBackendCommand() {
  if (app.isPackaged) {
    const exe = process.platform === 'win32' ? 'jarvis-backend.exe' : 'jarvis-backend';
    return { cmd: path.join(process.resourcesPath, 'backend', exe), args: ['--port', '0'] };
  }
  // Dev mode: run from the venv in the repo root
  const repoRoot = path.join(__dirname, '..');
  const venv = process.platform === 'win32'
    ? path.join(repoRoot, 'venv', 'Scripts', 'python.exe')
    : path.join(repoRoot, 'venv', 'bin', 'python');
  const script = path.join(repoRoot, 'src', 'server.py');
  return { cmd: venv, args: ['-u', script, '--port', '0'] };
}

function checkExistingBackend() {
  // Dev convenience: if run.sh (or the user) already started the backend on
  // 8765, reuse it instead of spawning a second instance.
  return new Promise((resolve) => {
    const timeout = setTimeout(() => resolve(false), 1000);
    const req = require('http').get({ host: '127.0.0.1', port: DEV_BACKEND_PORT, path: '/api/health', timeout: 900 }, (res) => {
      clearTimeout(timeout);
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => {
      clearTimeout(timeout);
      resolve(false);
    });
    req.on('timeout', () => {
      clearTimeout(timeout);
      req.destroy();
      resolve(false);
    });
  });
}

function startBackend() {
  const { cmd, args } = resolveBackendCommand();
  if (!fs.existsSync(cmd)) {
    console.error('Backend not found:', cmd);
    backendUrl = `http://127.0.0.1:${DEV_BACKEND_PORT}`;
    return;
  }
  backendProcess = spawn(cmd, args, {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      ...process.env,
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
      ...(apiToken ? { JARVIS_API_TOKEN: apiToken } : {})
    }
  });

  const onOutput = (buf) => {
    const text = buf.toString();
    console.log('[backend]', text.trim());
    const m = text.match(/JARVIS_PORT=(\d+)/);
    if (m) {
      backendUrl = `http://127.0.0.1:${m[1]}`;
      console.log('Backend ready at', backendUrl);
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('backend:url', backendUrl);
      }
    }
  };
  backendProcess.stdout.on('data', onOutput);
  backendProcess.stderr.on('data', onOutput);
  backendProcess.on('error', (e) => console.error('Backend spawn error:', e));
  backendProcess.on('exit', (code) => {
    console.log('Backend exited:', code);
    backendProcess = null;
    backendUrl = null;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend:url', null);
    }
  });
}

app.whenReady().then(async () => {
  apiToken = ensureApiToken();   // before the backend starts: it gets the token via env
  if (app.isPackaged || !(await checkExistingBackend())) {
    startBackend();
  } else {
    backendUrl = `http://127.0.0.1:${DEV_BACKEND_PORT}`;
    console.log('Reusing existing backend on 8765');
  }
  createWindow();
  registerGlobalShortcuts();
  setupAutoUpdater();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

/* ---------------- auto updater ----------------
   Checks the GitHub release feed (latest.yml generated by electron-builder and
   uploaded by `npm run publish`). Downloads in the background; installs when
   the app quits. Only active in packaged builds. */
let updateDownloaded = false;
let updateQuitting = false;

function setupAutoUpdater() {
  if (!app.isPackaged) return;
  autoUpdater.autoDownload = true;
  autoUpdater.on('update-available', (info) => {
    console.log('Update available:', info.version);
    if (mainWindow) mainWindow.webContents.send('update:available', info.version);
  });
  autoUpdater.on('download-progress', (p) => {
    if (mainWindow) mainWindow.webContents.send('update:progress', Math.round(p.percent));
  });
  autoUpdater.on('update-downloaded', (info) => {
    updateDownloaded = true;
    console.log('Update downloaded:', info.version);
    if (mainWindow) mainWindow.webContents.send('update:downloaded', info.version);
  });
  autoUpdater.on('error', (e) => {
    console.warn('auto-update error:', e.message);
  });
  autoUpdater.checkForUpdates().catch((e) => console.warn('update check failed:', e.message));
  setInterval(() => {
    autoUpdater.checkForUpdates().catch(() => {});
  }, 4 * 60 * 60 * 1000); // re-check every 4h
}

app.on('before-quit', () => {
  if (updateDownloaded && !updateQuitting) {
    updateQuitting = true;
    // silent install, relaunch after finish; app exits here
    autoUpdater.quitAndInstall(true, true);
  }
});

function stopBackend() {
  if (!backendProcess) return;
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(backendProcess.pid), '/T', '/F']);
    } else {
      backendProcess.kill('SIGKILL');
    }
  } catch (e) {
    console.warn('Backend stop failed:', e.message);
  }
  backendProcess = null;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 860,
    height: 540,
    minWidth: 720,
    minHeight: 460,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    backgroundColor: '#00000000',
    resizable: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function registerGlobalShortcuts() {
  try {
    globalShortcut.register('`', () => {
      if (mainWindow) {
        mainWindow.webContents.send('task:clear');
        mainWindow.focus();
      }
    });
    globalShortcut.register('Escape', () => {
      if (mainWindow) mainWindow.webContents.send('task:clear');
    });
  } catch (e) {
    console.warn('Global shortcut registration failed:', e.message);
  }
}

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  stopBackend();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

ipcMain.handle('window:minimize', () => {
  if (mainWindow) mainWindow.minimize();
});

ipcMain.handle('window:close', () => {
  if (mainWindow) mainWindow.close();
});

ipcMain.handle('window:focus', () => {
  if (mainWindow) mainWindow.focus();
});

ipcMain.handle('get-backend-url', () => backendUrl);

// The renderer authenticates with this token; IPC only, never over HTTP.
ipcMain.handle('get-api-token', () => apiToken || null);

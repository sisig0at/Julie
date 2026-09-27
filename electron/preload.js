const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jarvis', {
  minimize: () => ipcRenderer.invoke('window:minimize'),
  close: () => ipcRenderer.invoke('window:close'),
  focus: () => ipcRenderer.invoke('window:focus'),
  getBackendUrl: () => ipcRenderer.invoke('get-backend-url'),
  getApiToken: () => ipcRenderer.invoke('get-api-token'),
  onBackendUrl: (cb) => ipcRenderer.on('backend:url', (_e, u) => cb(u)),
  onTaskClear: (cb) => ipcRenderer.on('task:clear', () => cb()),
  onUpdateAvailable: (cb) => ipcRenderer.on('update:available', (_e, v) => cb(v)),
  onUpdateProgress: (cb) => ipcRenderer.on('update:progress', (_e, p) => cb(p)),
  onUpdateDownloaded: (cb) => ipcRenderer.on('update:downloaded', (_e, v) => cb(v))
});

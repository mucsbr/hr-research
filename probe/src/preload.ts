import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('probe', {
  status: () => ipcRenderer.invoke('probe:status'),
  register: (input: unknown) => ipcRenderer.invoke('probe:register', input),
  pause: (value: boolean) => ipcRenderer.invoke('probe:pause', value),
  reconnect: () => ipcRenderer.invoke('probe:reconnect'),
  openLogin: (input: unknown) => ipcRenderer.invoke('probe:open-login', input),
  confirmLogin: () => ipcRenderer.invoke('probe:confirm-login'),
  unpair: () => ipcRenderer.invoke('probe:unpair'),
  startup: (value: boolean) => ipcRenderer.invoke('probe:startup', value),
  onState: (listener: (value: unknown) => void) => {
    const handler = (_event: unknown, value: unknown) => listener(value);
    ipcRenderer.on('probe:state', handler); return () => ipcRenderer.removeListener('probe:state', handler);
  },
});

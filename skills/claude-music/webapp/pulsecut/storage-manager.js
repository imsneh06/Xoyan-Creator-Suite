/**
 * PulseCut: Storage Manager
 * Uses Origin Private File System (OPFS) and IndexedDB for persistent offline caching.
 * 100% Free, Client-Side, Zero Dependencies
 */

export class StorageManager {
  constructor(dbName = 'PulseCutDB') {
    this.dbName = dbName;
    this.opfsRoot = null;
    this._initOpfs();
  }

  async _initOpfs() {
    if (navigator.storage && navigator.storage.getDirectory) {
      try {
        this.opfsRoot = await navigator.storage.getDirectory();
      } catch (e) {
        console.warn('[StorageManager] OPFS unavailable, fallback to IndexedDB:', e);
      }
    }
  }

  async openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(this.dbName, 1);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains('projects')) {
          db.createObjectStore('projects', { keyPath: 'id' });
        }
        if (!db.objectStoreNames.contains('assets')) {
          db.createObjectStore('assets', { keyPath: 'id' });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  /**
   * Save full project timeline JSON
   */
  async saveProject(timelineState) {
    const db = await this.openDb();
    const data = JSON.parse(timelineState.serialize());
    return new Promise((resolve, reject) => {
      const tx = db.transaction('projects', 'readwrite');
      const store = tx.objectStore('projects');
      const req = store.put(data);
      req.onsuccess = () => resolve(true);
      req.onerror = () => reject(req.error);
    });
  }

  /**
   * Load the most recently modified project
   */
  async loadLatestProject() {
    const db = await this.openDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('projects', 'readonly');
      const store = tx.objectStore('projects');
      const req = store.getAll();
      req.onsuccess = () => {
        const list = req.result || [];
        if (!list.length) return resolve(null);
        list.sort((a, b) => (b.project?.updated_at || 0) - (a.project?.updated_at || 0));
        resolve(list[0]);
      };
      req.onerror = () => reject(req.error);
    });
  }

  /**
   * Store raw binary Blob into OPFS
   */
  async saveMediaToOpfs(fileName, blob) {
    if (!this.opfsRoot) await this._initOpfs();
    if (!this.opfsRoot) return null;

    try {
      const fileHandle = await this.opfsRoot.getFileHandle(fileName, { create: true });
      const writable = await fileHandle.createWritable();
      await writable.write(blob);
      await writable.close();
      return fileName;
    } catch (err) {
      console.warn('[StorageManager] write to OPFS failed:', err);
      return null;
    }
  }

  /**
   * Retrieve raw binary Blob from OPFS
   */
  async getMediaFromOpfs(fileName) {
    if (!this.opfsRoot) await this._initOpfs();
    if (!this.opfsRoot) return null;

    try {
      const fileHandle = await this.opfsRoot.getFileHandle(fileName);
      return await fileHandle.getFile();
    } catch (err) {
      return null;
    }
  }
}

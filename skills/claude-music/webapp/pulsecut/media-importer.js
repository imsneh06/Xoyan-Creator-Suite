/**
 * PulseCut: Media Importer & Proxy Generator
 * Extracts frame dimensions, duration, and generates 360p mobile preview proxies.
 * 100% Free, Client-Side, Zero Dependencies
 */

export class MediaImporter {
  constructor() {
    this.proxyCache = new Map();
  }

  /**
   * Import a File object or URL into an Asset record.
   */
  async importFile(fileOrUrl, nameOverride = '') {
    if (typeof fileOrUrl === 'string') {
      return this.importFromUrl(fileOrUrl, nameOverride);
    }
    return this.importFromFile(fileOrUrl, nameOverride);
  }

  async importFromFile(file, nameOverride = '') {
    const isVideo = file.type.startsWith('video/');
    const isAudio = file.type.startsWith('audio/');
    const isImage = file.type.startsWith('image/');
    const type = isVideo ? 'video' : isAudio ? 'audio' : isImage ? 'image' : 'video';

    const blobUrl = URL.createObjectURL(file);
    const meta = await this._probeMedia(blobUrl, type);

    const asset = {
      id: 'asset_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
      name: nameOverride || file.name || 'Imported ' + type,
      type: type,
      duration: meta.duration,
      width: meta.width || 1080,
      height: meta.height || 1920,
      sample_rate: meta.sample_rate || 44100,
      channels: 2,
      url: blobUrl,
      proxy_url: blobUrl,
      file: file,
    };

    // If video is high-res (> 1080p), generate mobile proxy thumbnail canvas
    if (isVideo && (meta.width > 1280 || meta.height > 1280)) {
      try {
        const proxyBlob = await this._generateLowResProxy(blobUrl, meta);
        if (proxyBlob) {
          asset.proxy_url = URL.createObjectURL(proxyBlob);
        }
      } catch (err) {
        console.warn('[MediaImporter] proxy generation warning:', err);
      }
    }

    return asset;
  }

  async importFromUrl(url, nameOverride = '') {
    const isAudio = /\.(mp3|wav|ogg|m4a|aac|flac)($|\?)/i.test(url);
    const type = isAudio ? 'audio' : 'video';
    const meta = await this._probeMedia(url, type);

    return {
      id: 'asset_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
      name: nameOverride || url.split('/').pop().split('?')[0] || 'Remote ' + type,
      type: type,
      duration: meta.duration,
      width: meta.width || 1080,
      height: meta.height || 1920,
      sample_rate: 44100,
      channels: 2,
      url: url,
      proxy_url: url,
    };
  }

  _probeMedia(url, type) {
    return new Promise((resolve) => {
      if (type === 'audio') {
        const a = document.createElement('audio');
        a.preload = 'metadata';
        a.src = url;
        a.onloadedmetadata = () => {
          resolve({ duration: a.duration || 10.0, width: 0, height: 0 });
        };
        a.onerror = () => {
          resolve({ duration: 30.0, width: 0, height: 0 });
        };
      } else {
        const v = document.createElement('video');
        v.preload = 'metadata';
        v.muted = true;
        v.playsInline = true;
        v.src = url;
        v.onloadedmetadata = () => {
          resolve({
            duration: v.duration || 10.0,
            width: v.videoWidth || 1080,
            height: v.videoHeight || 1920,
          });
        };
        v.onerror = () => {
          resolve({ duration: 15.0, width: 1080, height: 1920 });
        };
      }
    });
  }

  /**
   * Generates a lightweight 360p poster frame or thumbnail strip to conserve mobile VRAM.
   */
  async _generateLowResProxy(url, meta) {
    return new Promise((resolve) => {
      const v = document.createElement('video');
      v.muted = true;
      v.playsInline = true;
      v.crossOrigin = 'anonymous';
      v.src = url;
      v.currentTime = Math.min(1.0, meta.duration / 2);

      v.onseeked = () => {
        const canvas = document.createElement('canvas');
        const targetW = 360;
        const targetH = Math.round(targetW * (meta.height / meta.width));
        canvas.width = targetW;
        canvas.height = targetH;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(v, 0, 0, targetW, targetH);
        canvas.toBlob((blob) => resolve(blob), 'image/jpeg', 0.7);
      };

      v.onerror = () => resolve(null);
    });
  }
}

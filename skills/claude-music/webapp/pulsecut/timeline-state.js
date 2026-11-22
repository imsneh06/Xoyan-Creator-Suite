/**
 * PulseCut: Universal Timeline State Manager
 * Adheres strictly to docs/timeline-schema.json
 * 100% Free, Client-Side, Zero Dependencies
 */

export class TimelineState {
  constructor(initialData = null) {
    this.listeners = new Set();
    this.history = [];
    this.historyIndex = -1;
    this.maxHistory = 50;

    this.project = initialData ? this._clone(initialData.project) : this.createDefaultProject();
    this.assets = initialData ? this._clone(initialData.assets) : [];
    this.tracks = initialData ? this._clone(initialData.tracks) : this.createDefaultTracks();

    this.recordState('Initial Project');
  }

  createDefaultProject(title = 'New Reel Project') {
    return {
      id: 'proj_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
      title: title,
      author: 'Creator',
      created_at: Date.now(),
      updated_at: Date.now(),
      canvas: {
        width: 1080,
        height: 1920,
        aspect_ratio: '9:16',
        fps: 30,
        duration: 0.0,
        background_color: '#000000',
      },
    };
  }

  createDefaultTracks() {
    return [
      {
        id: 'track_video_main',
        name: 'Main Video Track',
        type: 'video',
        order: 0,
        muted: false,
        locked: false,
        visible: true,
        volume: 1.0,
        clips: [],
      },
      {
        id: 'track_overlay_stickers',
        name: 'Stickers & Graphics',
        type: 'sticker',
        order: 1,
        muted: false,
        locked: false,
        visible: true,
        volume: 1.0,
        clips: [],
      },
      {
        id: 'track_subtitles',
        name: 'Captions & Dynamic Text',
        type: 'subtitle',
        order: 2,
        muted: false,
        locked: false,
        visible: true,
        volume: 1.0,
        clips: [],
      },
      {
        id: 'track_audio_bgm',
        name: 'Background Music',
        type: 'audio',
        order: 3,
        muted: false,
        locked: false,
        visible: true,
        volume: 0.35,
        clips: [],
      },
    ];
  }

  // Reactive Subscription
  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  notify(actionName = 'update') {
    this.project.updated_at = Date.now();
    this._recalculateDuration();
    for (const listener of this.listeners) {
      try {
        listener(this, actionName);
      } catch (err) {
        console.error('[TimelineState] listener error:', err);
      }
    }
  }

  // History & Undo / Redo
  recordState(label = 'Edit') {
    if (this.historyIndex < this.history.length - 1) {
      this.history = this.history.slice(0, this.historyIndex + 1);
    }
    const snap = this.serialize();
    this.history.push({ label, snap });
    if (this.history.length > this.maxHistory) {
      this.history.shift();
    } else {
      this.historyIndex++;
    }
    this.notify(label);
  }

  undo() {
    if (this.historyIndex > 0) {
      this.historyIndex--;
      const { snap, label } = this.history[this.historyIndex];
      this.deserialize(snap, false);
      this.notify('undo: ' + label);
      return true;
    }
    return false;
  }

  redo() {
    if (this.historyIndex < this.history.length - 1) {
      this.historyIndex++;
      const { snap, label } = this.history[this.historyIndex];
      this.deserialize(snap, false);
      this.notify('redo: ' + label);
      return true;
    }
    return false;
  }

  canUndo() {
    return this.historyIndex > 0;
  }

  canRedo() {
    return this.historyIndex < this.history.length - 1;
  }

  // Assets Management
  addAsset(asset) {
    const existing = this.assets.find(a => a.id === asset.id);
    if (existing) return existing;
    const cleanAsset = {
      id: asset.id || 'asset_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
      name: asset.name || 'Media Asset',
      type: asset.type || 'video',
      duration: Math.max(0.1, parseFloat(asset.duration) || 0),
      width: asset.width || 1080,
      height: asset.height || 1920,
      sample_rate: asset.sample_rate || 44100,
      channels: asset.channels || 2,
      url: asset.url || '',
      proxy_url: asset.proxy_url || asset.url || '',
      file: asset.file || null,
    };
    this.assets.push(cleanAsset);
    this.recordState('Add Asset');
    return cleanAsset;
  }

  getAsset(assetId) {
    return this.assets.find(a => a.id === assetId) || null;
  }

  // Tracks Management
  getTrack(trackId) {
    return this.tracks.find(t => t.id === trackId) || null;
  }

  getTrackByType(type) {
    return this.tracks.find(t => t.type === type) || null;
  }

  addTrack(track) {
    const newTrack = {
      id: track.id || 'track_' + Date.now(),
      name: track.name || 'Track ' + (this.tracks.length + 1),
      type: track.type || 'video',
      order: this.tracks.length,
      muted: !!track.muted,
      locked: !!track.locked,
      visible: track.visible !== false,
      volume: track.volume !== undefined ? track.volume : 1.0,
      clips: [],
    };
    this.tracks.push(newTrack);
    this.recordState('Add Track');
    return newTrack;
  }

  // Clips Management
  getClip(clipId) {
    for (const track of this.tracks) {
      const c = track.clips.find(x => x.id === clipId);
      if (c) return { clip: c, track };
    }
    return null;
  }

  addClip(trackId, clipData) {
    const track = this.getTrack(trackId);
    if (!track) throw new Error(`Track ${trackId} not found`);

    const dur = clipData.end - clipData.start;
    const cleanClip = {
      id: clipData.id || 'clip_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
      asset_id: clipData.asset_id || null,
      start: Math.max(0, clipData.start || 0),
      end: Math.max(clipData.start + 0.1, clipData.end || clipData.start + 5.0),
      trim_in: Math.max(0, clipData.trim_in || 0),
      trim_out: clipData.trim_out !== undefined ? clipData.trim_out : Math.max(0, clipData.trim_in || 0) + dur,
      speed: Math.max(0.1, Math.min(8.0, clipData.speed || 1.0)),
      volume: clipData.volume !== undefined ? clipData.volume : 1.0,
      muted: !!clipData.muted,
      transform: {
        x: 0,
        y: 0,
        scale: 1.0,
        rotation: 0,
        opacity: 1.0,
        fit: 'cover',
        ...(clipData.transform || {}),
      },
      text: clipData.text ? { ...clipData.text } : null,
      captions: clipData.captions ? { ...clipData.captions } : null,
      effects: Array.isArray(clipData.effects) ? [...clipData.effects] : [],
      transition: clipData.transition ? { ...clipData.transition } : null,
    };

    track.clips.push(cleanClip);
    track.clips.sort((a, b) => a.start - b.start);
    this.recordState('Add Clip');
    return cleanClip;
  }

  removeClip(clipId) {
    for (const track of this.tracks) {
      const idx = track.clips.findIndex(c => c.id === clipId);
      if (idx >= 0) {
        track.clips.splice(idx, 1);
        this.recordState('Remove Clip');
        return true;
      }
    }
    return false;
  }

  // Clip Operations: Split, Trim, Speed, Volume
  splitClip(clipId, time) {
    const item = this.getClip(clipId);
    if (!item) return null;
    const { clip, track } = item;

    if (time <= clip.start + 0.05 || time >= clip.end - 0.05) {
      return null; // Don't split too close to edges
    }

    const elapsedTimeline = time - clip.start;
    const elapsedSource = elapsedTimeline * clip.speed;
    const splitSourceTime = clip.trim_in + elapsedSource;

    const originalEnd = clip.end;
    const originalTrimOut = clip.trim_out;

    // Mutate first half
    clip.end = time;
    clip.trim_out = splitSourceTime;

    // Create second half
    const newClip = {
      ...this._clone(clip),
      id: 'clip_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
      start: time,
      end: originalEnd,
      trim_in: splitSourceTime,
      trim_out: originalTrimOut,
    };

    track.clips.push(newClip);
    track.clips.sort((a, b) => a.start - b.start);
    this.recordState('Split Clip');
    return { left: clip, right: newClip };
  }

  trimClip(clipId, { trim_in, trim_out, start, end }) {
    const item = this.getClip(clipId);
    if (!item) return false;
    const { clip, track } = item;

    if (start !== undefined) clip.start = Math.max(0, start);
    if (end !== undefined) clip.end = Math.max(clip.start + 0.1, end);
    if (trim_in !== undefined) clip.trim_in = Math.max(0, trim_in);
    if (trim_out !== undefined) clip.trim_out = Math.max(clip.trim_in + 0.1, trim_out);

    track.clips.sort((a, b) => a.start - b.start);
    this.recordState('Trim Clip');
    return true;
  }

  setClipSpeed(clipId, newSpeed) {
    const item = this.getClip(clipId);
    if (!item) return false;
    const { clip } = item;

    const speed = Math.max(0.1, Math.min(8.0, parseFloat(newSpeed) || 1.0));
    const sourceDuration = clip.trim_out - clip.trim_in;
    const newTimelineDuration = sourceDuration / speed;

    clip.speed = speed;
    clip.end = clip.start + newTimelineDuration;
    this.recordState('Change Speed');
    return true;
  }

  setClipVolume(clipId, volume, muted = null) {
    const item = this.getClip(clipId);
    if (!item) return false;
    const { clip } = item;

    clip.volume = Math.max(0.0, Math.min(2.0, parseFloat(volume) || 0.0));
    if (muted !== null) clip.muted = !!muted;
    this.recordState('Adjust Volume');
    return true;
  }

  setClipEffect(clipId, effectType, params = {}) {
    const item = this.getClip(clipId);
    if (!item) return false;
    const { clip } = item;

    clip.effects = clip.effects.filter(e => e.type !== effectType);
    if (effectType && effectType !== 'clean' && effectType !== 'none') {
      clip.effects.push({ type: effectType, params });
    }
    this.recordState('Set Effect: ' + effectType);
    return true;
  }

  setAspectRatio(ratio) {
    const valid = ['9:16', '1:1', '4:5', '16:9'];
    if (!valid.includes(ratio)) return;

    this.project.canvas.aspect_ratio = ratio;
    if (ratio === '9:16') {
      this.project.canvas.width = 1080;
      this.project.canvas.height = 1920;
    } else if (ratio === '1:1') {
      this.project.canvas.width = 1080;
      this.project.canvas.height = 1080;
    } else if (ratio === '4:5') {
      this.project.canvas.width = 1080;
      this.project.canvas.height = 1350;
    } else if (ratio === '16:9') {
      this.project.canvas.width = 1920;
      this.project.canvas.height = 1080;
    }
    this.recordState('Aspect Ratio ' + ratio);
  }

  // Active Clips at current playback timestamp
  getActiveClipsAt(time) {
    const active = [];
    for (const track of this.tracks) {
      if (!track.visible && track.type !== 'audio') continue;
      for (const clip of track.clips) {
        if (time >= clip.start && time < clip.end) {
          active.push({ clip, track });
        }
      }
    }
    return active;
  }

  // Internal Recalculation
  _recalculateDuration() {
    let maxEnd = 0;
    for (const track of this.tracks) {
      for (const clip of track.clips) {
        if (clip.end > maxEnd) maxEnd = clip.end;
      }
    }
    this.project.canvas.duration = Math.max(1.0, maxEnd);
  }

  _clone(obj) {
    return JSON.parse(JSON.stringify(obj));
  }

  serialize() {
    return JSON.stringify({
      version: '1.0.0',
      project: this.project,
      assets: this.assets.map(a => ({ ...a, file: undefined })), // Omit native File objects
      tracks: this.tracks,
    }, null, 2);
  }

  deserialize(jsonString, record = true) {
    const data = typeof jsonString === 'string' ? JSON.parse(jsonString) : jsonString;
    this.project = data.project || this.createDefaultProject();
    this.assets = data.assets || [];
    this.tracks = data.tracks || this.createDefaultTracks();
    this._recalculateDuration();
    if (record) {
      this.recordState('Load Project');
    }
  }
}

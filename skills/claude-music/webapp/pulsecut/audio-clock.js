/**
 * PulseCut: Web Audio API Master Clock & Real-time Mixer
 * Eliminates audio/video drift by using AudioContext.currentTime as the master authority.
 * 100% Free, Client-Side, Zero Dependencies
 */

export class AudioClock {
  constructor() {
    this.ctx = null;
    this.isPlaying = false;
    this.currentTime = 0.0;
    this.duration = 0.0;
    this.playbackRate = 1.0;

    this._startCtxTime = 0.0;
    this._startTimelineTime = 0.0;
    this._listeners = new Set();
    this._activeAudioNodes = new Map();

    this.masterGain = null;
  }

  ensureContext() {
    if (!this.ctx) {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AudioCtx();
      this.masterGain = this.ctx.createGain();
      this.masterGain.gain.setValueAtTime(1.0, this.ctx.currentTime);
      this.masterGain.connect(this.ctx.destination);
    }
    if (this.ctx.state === 'suspended') {
      this.ctx.resume();
    }
  }

  play(startTime = null) {
    this.ensureContext();
    if (this.isPlaying) return;

    if (startTime !== null) {
      this.currentTime = Math.max(0, startTime);
    }

    if (this.currentTime >= this.duration && this.duration > 0) {
      this.currentTime = 0;
    }

    this._startCtxTime = this.ctx.currentTime;
    this._startTimelineTime = this.currentTime;
    this.isPlaying = true;
    this._notify('play');
  }

  pause() {
    if (!this.isPlaying) return;
    this.currentTime = this.getCurrentTime();
    this.isPlaying = false;
    this._stopAllAudioNodes();
    this._notify('pause');
  }

  seek(time) {
    const clamped = Math.max(0, Math.min(this.duration || 9999, parseFloat(time) || 0));
    this.currentTime = clamped;
    if (this.isPlaying && this.ctx) {
      this._startCtxTime = this.ctx.currentTime;
      this._startTimelineTime = this.currentTime;
    }
    this._notify('seek');
  }

  getCurrentTime() {
    if (!this.isPlaying || !this.ctx) {
      return this.currentTime;
    }
    const elapsed = (this.ctx.currentTime - this._startCtxTime) * this.playbackRate;
    const computed = this._startTimelineTime + elapsed;
    if (this.duration > 0 && computed >= this.duration) {
      this.currentTime = this.duration;
      this.pause();
      return this.duration;
    }
    return computed;
  }

  setDuration(dur) {
    this.duration = Math.max(0.1, parseFloat(dur) || 0.1);
  }

  setPlaybackRate(rate) {
    const r = Math.max(0.25, Math.min(4.0, parseFloat(rate) || 1.0));
    if (this.isPlaying) {
      this.currentTime = this.getCurrentTime();
      this._startCtxTime = this.ctx.currentTime;
      this._startTimelineTime = this.currentTime;
    }
    this.playbackRate = r;
    this._notify('rate');
  }

  setMasterVolume(vol) {
    if (!this.masterGain || !this.ctx) return;
    const v = Math.max(0.0, Math.min(2.0, parseFloat(vol) || 0.0));
    this.masterGain.gain.setValueAtTime(v, this.ctx.currentTime);
  }

  subscribe(fn) {
    this._listeners.add(fn);
    return () => this._listeners.delete(fn);
  }

  _notify(event) {
    for (const fn of this._listeners) {
      try {
        fn(this.getCurrentTime(), event, this);
      } catch (err) {
        console.error('[AudioClock] error in listener:', err);
      }
    }
  }

  _stopAllAudioNodes() {
    for (const node of this._activeAudioNodes.values()) {
      try {
        if (node.source) node.source.stop();
        if (node.source) node.source.disconnect();
      } catch (e) {}
    }
    this._activeAudioNodes.clear();
  }
}

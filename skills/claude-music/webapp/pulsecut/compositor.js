/**
 * PulseCut: Canvas2D / WebGL Multi-Layer Real-Time Compositor
 * Evaluates active tracks, video frames, dynamic text, captions, and shader filters.
 * 100% Free, Client-Side, Zero Dependencies
 */

export class Compositor {
  constructor(canvasElement, timelineState, audioClock) {
    this.canvas = canvasElement;
    this.ctx = canvasElement.getContext('2d');
    this.state = timelineState;
    this.clock = audioClock;

    this._mediaElements = new Map(); // assetId -> HTMLVideoElement / HTMLImageElement
    this._rafId = null;
    this._isRunning = false;

    // Offscreen staging canvas for applying visual effect shaders & color grades
    this.stagingCanvas = document.createElement('canvas');
    this.stagingCtx = this.stagingCanvas.getContext('2d');

    this._setupClockListeners();
  }

  attachCanvas(canvasElement) {
    if (!canvasElement) return;
    this.canvas = canvasElement;
    this.ctx = canvasElement.getContext('2d');
  }

  _setupClockListeners() {
    this.clock.subscribe((time, event) => {
      if (event === 'seek' || event === 'pause') {
        this.renderFrame(time);
        this._syncMediaElements(time, false);
      } else if (event === 'play') {
        this._syncMediaElements(time, true);
      }
    });
  }

  start() {
    if (this._isRunning) return;
    this._isRunning = true;
    const loop = () => {
      if (!this._isRunning) return;
      const t = this.clock.getCurrentTime();
      this.renderFrame(t);
      if (this.clock.isPlaying) {
        this._syncMediaElements(t, true);
      }
      this._rafId = requestAnimationFrame(loop);
    };
    this._rafId = requestAnimationFrame(loop);
  }

  stop() {
    this._isRunning = false;
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
    this._pauseAllMediaElements();
  }

  setCanvasSize(width, height) {
    this.canvas.width = width;
    this.canvas.height = height;
    this.stagingCanvas.width = width;
    this.stagingCanvas.height = height;
  }

  /**
   * Main rendering pass for a single frame at timestamp `time`
   */
  renderFrame(time) {
    const cWidth = this.canvas.width || 1080;
    const cHeight = this.canvas.height || 1920;

    // 1. Clear background
    this.ctx.fillStyle = this.state.project.canvas.background_color || '#000000';
    this.ctx.fillRect(0, 0, cWidth, cHeight);

    // 2. Query active clips from all visible tracks
    const activeItems = this.state.getActiveClipsAt(time);

    // Sort by track order (bottom-to-top visual stacking)
    activeItems.sort((a, b) => a.track.order - b.track.order);

    for (const { clip, track } of activeItems) {
      if (!track.visible && track.type !== 'audio') continue;

      if (track.type === 'video' || track.type === 'image') {
        this._renderVideoClip(clip, time, cWidth, cHeight);
      } else if (track.type === 'sticker') {
        this._renderStickerClip(clip, time, cWidth, cHeight);
      } else if (track.type === 'subtitle') {
        this._renderCaptionClip(clip, time, cWidth, cHeight);
      } else if (track.type === 'text') {
        this._renderTextClip(clip, time, cWidth, cHeight);
      }
    }
  }

  _renderVideoClip(clip, time, cWidth, cHeight) {
    const asset = this.state.getAsset(clip.asset_id);
    if (!asset) return;

    let mediaEl = this._getOrCreateMediaElement(asset);
    if (!mediaEl || mediaEl.readyState < 2) return; // Need at least HAVE_CURRENT_DATA

    const { stagingCanvas, stagingCtx, ctx } = this;
    stagingCtx.clearRect(0, 0, cWidth, cHeight);

    // Compute fit geometry (cover / contain)
    const srcW = mediaEl.videoWidth || asset.width || 1080;
    const srcH = mediaEl.videoHeight || asset.height || 1920;
    const { dx, dy, dw, dh } = this._computeFit(srcW, srcH, cWidth, cHeight, clip.transform?.fit || 'cover');

    stagingCtx.save();
    if (clip.transform) {
      const t = clip.transform;
      stagingCtx.globalAlpha = t.opacity !== undefined ? t.opacity : 1.0;
      if (t.rotation) {
        stagingCtx.translate(cWidth / 2, cHeight / 2);
        stagingCtx.rotate((t.rotation * Math.PI) / 180);
        stagingCtx.translate(-cWidth / 2, -cHeight / 2);
      }
    }

    stagingCtx.drawImage(mediaEl, dx, dy, dw, dh);
    stagingCtx.restore();

    // Apply visual effects stack (VHS, grain, glow, teal/orange, b&w)
    this._applyEffects(stagingCtx, clip.effects, cWidth, cHeight, time);

    // Apply In/Out Transitions
    let transitionAlpha = 1.0;
    if (clip.transition) {
      const dur = clip.end - clip.start;
      const elapsed = time - clip.start;
      if (clip.transition.in && elapsed < clip.transition.in.duration) {
        transitionAlpha = Math.max(0, elapsed / clip.transition.in.duration);
      } else if (clip.transition.out && clip.end - time < clip.transition.out.duration) {
        transitionAlpha = Math.max(0, (clip.end - time) / clip.transition.out.duration);
      }
    }

    ctx.save();
    ctx.globalAlpha = transitionAlpha;
    ctx.drawImage(stagingCanvas, 0, 0);
    ctx.restore();
  }

  _renderStickerClip(clip, time, cWidth, cHeight) {
    const textData = clip.text;
    if (!textData || !textData.content) return;

    const ctx = this.ctx;
    ctx.save();
    ctx.font = '800 44px Montserrat, Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    const text = textData.content;
    const textMetrics = ctx.measureText(text);
    const boxW = textMetrics.width + 48;
    const boxH = 68;

    let posX = cWidth / 2;
    let posY = 180;
    if (textData.position === 'middle') posY = cHeight / 2;
    if (textData.position === 'bottom') posY = cHeight - 260;

    // Draw pill badge background
    ctx.fillStyle = textData.background_color || 'rgba(18, 18, 18, 0.88)';
    ctx.strokeStyle = textData.border_color || '#3b82f6';
    ctx.lineWidth = 3;
    this._roundRect(ctx, posX - boxW / 2, posY - boxH / 2, boxW, boxH, 14, true, true);

    // Text label
    ctx.fillStyle = textData.color || '#ffffff';
    ctx.fillText(text, posX, posY + 2);
    ctx.restore();
  }

  _renderCaptionClip(clip, time, cWidth, cHeight) {
    const caps = clip.captions;
    if (!caps || !caps.words || !caps.words.length) return;

    // Active words at current timeline time
    const relTime = time - clip.start + clip.trim_in;
    const activeWord = caps.words.find(w => relTime >= w.start && relTime <= w.end);

    // Get current chunk of 3-4 words for context
    const currentWords = caps.words.filter(w => Math.abs(w.start - relTime) < 1.2);
    if (!currentWords.length) return;

    const ctx = this.ctx;
    ctx.save();
    ctx.font = '900 64px Bangers, Montserrat, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    const posX = cWidth / 2;
    const posY = cHeight * 0.72; // Standard 72% vertical caption position

    // Draw words
    const phrase = currentWords.map(w => w.word).join(' ').toUpperCase();
    const metrics = ctx.measureText(phrase);
    const boxW = metrics.width + 60;
    const boxH = 92;

    // Dark backdrop for maximum legibility
    ctx.fillStyle = 'rgba(0, 0, 0, 0.75)';
    this._roundRect(ctx, posX - boxW / 2, posY - boxH / 2, boxW, boxH, 16, true, false);

    // Text with stroke outline
    ctx.lineWidth = 10;
    ctx.strokeStyle = '#000000';
    ctx.lineJoin = 'round';
    ctx.strokeText(phrase, posX, posY + 4);

    // Hormozi Style Pop Highlight
    ctx.fillStyle = activeWord ? '#facc15' : '#ffffff';
    ctx.fillText(phrase, posX, posY + 4);

    ctx.restore();
  }

  _renderTextClip(clip, time, cWidth, cHeight) {
    const t = clip.text;
    if (!t || !t.content) return;

    const ctx = this.ctx;
    ctx.save();
    ctx.font = `${t.font_weight || '700'} ${t.font_size || 48}px ${t.font_family || 'Montserrat, sans-serif'}`;
    ctx.textAlign = t.align || 'center';
    ctx.fillStyle = t.color || '#ffffff';
    ctx.strokeStyle = t.border_color || '#000000';
    ctx.lineWidth = t.border_width || 2;

    const posX = cWidth / 2;
    const posY = t.position === 'top' ? 240 : t.position === 'middle' ? cHeight / 2 : cHeight - 320;

    ctx.strokeText(t.content, posX, posY);
    ctx.fillText(t.content, posX, posY);
    ctx.restore();
  }

  _applyEffects(ctx, effects, width, height, time) {
    if (!effects || !effects.length) return;

    for (const eff of effects) {
      if (eff.type === 'bw_noir') {
        const imgData = ctx.getImageData(0, 0, width, height);
        const data = imgData.data;
        for (let i = 0; i < data.length; i += 4) {
          const luma = data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
          data[i] = luma;
          data[i + 1] = luma;
          data[i + 2] = luma;
        }
        ctx.putImageData(imgData, 0, 0);
      } else if (eff.type === 'film_grain') {
        // High-efficiency animated film grain
        ctx.save();
        ctx.fillStyle = (Math.sin(time * 50) > 0) ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)';
        for (let y = 0; y < height; y += 4) {
          if (Math.random() > 0.4) {
            ctx.fillRect(Math.random() * width, y, Math.random() * 20 + 2, 2);
          }
        }
        ctx.restore();
      } else if (eff.type === 'vhs') {
        ctx.save();
        ctx.fillStyle = 'rgba(255, 0, 60, 0.04)';
        ctx.fillRect(0, 0, width, height);
        ctx.fillStyle = 'rgba(0, 240, 255, 0.04)';
        ctx.fillRect(2, 2, width, height);
        ctx.restore();
      }
    }
  }

  _computeFit(srcW, srcH, targetW, targetH, fitMode) {
    const srcRatio = srcW / srcH;
    const targetRatio = targetW / targetH;

    let dw, dh, dx, dy;
    if (fitMode === 'cover') {
      if (srcRatio > targetRatio) {
        dh = targetH;
        dw = dh * srcRatio;
        dx = (targetW - dw) / 2;
        dy = 0;
      } else {
        dw = targetW;
        dh = dw / srcRatio;
        dx = 0;
        dy = (targetH - dh) / 2;
      }
    } else { // contain
      if (srcRatio > targetRatio) {
        dw = targetW;
        dh = dw / srcRatio;
        dx = 0;
        dy = (targetH - dh) / 2;
      } else {
        dh = targetH;
        dw = dh * srcRatio;
        dx = (targetW - dw) / 2;
        dy = 0;
      }
    }
    return { dx, dy, dw, dh };
  }

  _roundRect(ctx, x, y, width, height, radius, fill, stroke) {
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.lineTo(x + width - radius, y);
    ctx.quadraticCurveTo(x + width, y, x + width, y + radius);
    ctx.lineTo(x + width, y + height - radius);
    ctx.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
    ctx.lineTo(x + radius, y + height);
    ctx.quadraticCurveTo(x, y + height, x, y + height - radius);
    ctx.lineTo(x, y + radius);
    ctx.quadraticCurveTo(x, y, x + radius, y);
    ctx.closePath();
    if (fill) ctx.fill();
    if (stroke) ctx.stroke();
  }

  _getOrCreateMediaElement(asset) {
    if (this._mediaElements.has(asset.id)) {
      return this._mediaElements.get(asset.id);
    }

    if (asset.type === 'video') {
      const v = document.createElement('video');
      v.muted = true;
      v.playsInline = true;
      v.crossOrigin = 'anonymous';
      v.preload = 'auto';
      v.src = asset.url;
      this._mediaElements.set(asset.id, v);
      return v;
    }
    return null;
  }

  _syncMediaElements(time, isPlaying) {
    const active = this.state.getActiveClipsAt(time);
    for (const { clip, track } of active) {
      if (track.type !== 'video') continue;
      const asset = this.state.getAsset(clip.asset_id);
      if (!asset) continue;

      const mediaEl = this._getOrCreateMediaElement(asset);
      if (!mediaEl) continue;

      const targetMediaTime = clip.trim_in + (time - clip.start) * clip.speed;
      if (Math.abs(mediaEl.currentTime - targetMediaTime) > 0.15) {
        mediaEl.currentTime = targetMediaTime;
      }
      mediaEl.playbackRate = clip.speed * this.clock.playbackRate;

      if (isPlaying && mediaEl.paused) {
        mediaEl.play().catch(() => {});
      } else if (!isPlaying && !mediaEl.paused) {
        mediaEl.pause();
      }
    }
  }

  _pauseAllMediaElements() {
    for (const el of this._mediaElements.values()) {
      if (el.pause) el.pause();
    }
  }
}

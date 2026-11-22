/**
 * PulseCut: Mobile Touch-Friendly Multi-Track Timeline & Inspector UI
 * Neutral, Original Design · 100% Client-Side
 */

export class EditorUI {
  constructor(containerEl, timelineState, audioClock, compositor, exportEngine, speechTranscriber) {
    this.container = containerEl;
    this.state = timelineState;
    this.clock = audioClock;
    this.compositor = compositor;
    this.exporter = exportEngine;
    this.transcriber = speechTranscriber;

    this.selectedClipId = null;
    this.zoomLevel = 60; // Pixels per second on timeline

    this._renderBaseLayout();
    this._bindEvents();
    this._setupStateSubscribers();
  }

  _renderBaseLayout() {
    this.container.innerHTML = `
      <div class="pulsecut-studio" style="display:flex; flex-direction:column; gap:12px; font-family:Inter,system-ui,sans-serif;">
        <!-- Top Studio Toolbar -->
        <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; padding-bottom:10px; border-bottom:1px solid var(--line);">
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="font-weight:800; font-size:15px; color:var(--brand); display:flex; align-items:center; gap:6px;">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              PulseCut Studio
            </span>
            <span id="pc-project-title" style="font-size:12px; color:var(--muted); font-weight:600;">Reel Project</span>
          </div>

          <div style="display:flex; align-items:center; gap:6px;">
            <button type="button" class="btn btn-icon" id="pc-undo-btn" title="Undo" style="padding:4px 8px; font-size:12px;">↩️</button>
            <button type="button" class="btn btn-icon" id="pc-redo-btn" title="Redo" style="padding:4px 8px; font-size:12px;">↪️</button>
            <button type="button" class="btn" id="pc-split-btn" style="font-size:12px; padding:4px 10px; font-weight:600;">✂️ Split</button>
            <button type="button" class="btn" id="pc-auto-captions-btn" style="font-size:12px; padding:4px 10px; background:var(--card); color:var(--brand); font-weight:600;">✨ AI Captions</button>
            <button type="button" class="btn btn-primary" id="pc-export-btn" style="font-size:12px; padding:5px 14px; font-weight:700;">⬇️ Export MP4</button>
          </div>
        </div>

        <!-- Middle Layout: Video Preview + Inspector -->
        <div style="display:flex; gap:16px; align-items:flex-start; flex-wrap:wrap;">
          <!-- Viewport Phone Frame -->
          <div class="phone-frame" style="width:250px; height:444px; position:relative; background:#000; border-radius:16px; overflow:hidden; border:1px solid var(--line-strong); box-shadow:var(--shadow);">
            <canvas id="pc-viewport-canvas" width="1080" height="1920" style="width:100%; height:100%; object-fit:cover; display:block;"></canvas>
            <div style="position:absolute; top:8px; left:8px; font-family:monospace; font-size:11px; background:rgba(0,0,0,0.65); padding:2px 6px; border-radius:4px; color:#fff;" id="pc-time-overlay">0:00.0</div>
            <div style="position:absolute; top:8px; right:8px; font-size:11px; background:rgba(0,0,0,0.65); padding:2px 6px; border-radius:4px; color:var(--brand);" id="pc-ratio-overlay">9:16</div>
            <!-- Play/Pause Floating Control -->
            <button type="button" id="pc-play-btn" style="position:absolute; bottom:12px; left:50%; transform:translateX(-50%); width:44px; height:44px; border-radius:50%; background:var(--brand); border:none; color:#000; display:flex; align-items:center; justify-content:center; cursor:pointer; box-shadow:0 4px 12px rgba(0,0,0,0.4);">
              <svg id="pc-play-icon" width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            </button>
          </div>

          <!-- Inspector & Clip Editing Controls -->
          <div style="flex:1; min-width:280px; background:var(--well); border:1px solid var(--line); border-radius:10px; padding:14px; display:flex; flex-direction:column; gap:12px;" id="pc-inspector">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <span class="eyebrow" style="margin:0; font-size:11px; color:var(--brand);">Clip Inspector</span>
              <span id="pc-selection-label" class="mono" style="font-size:11px; color:var(--muted);">No clip selected</span>
            </div>

            <!-- Aspect Ratio Selector -->
            <div>
              <span class="field-label" style="display:block; margin-bottom:4px; font-size:11px;">Aspect Ratio:</span>
              <div class="ig-pill-group" id="pc-ratio-pills">
                <button type="button" class="ig-pill active" data-ratio="9:16">📱 9:16</button>
                <button type="button" class="ig-pill" data-ratio="1:1">⬛ 1:1</button>
                <button type="button" class="ig-pill" data-ratio="4:5">🖼️ 4:5</button>
                <button type="button" class="ig-pill" data-ratio="16:9">🖥️ 16:9</button>
              </div>
            </div>

            <!-- Trimming In/Out Steppers -->
            <div style="display:flex; gap:10px; flex-wrap:wrap;">
              <div class="field" style="margin:0;">
                <label class="field-label" style="margin:0; font-size:11px;">Trim In (s):</label>
                <input type="number" id="pc-trim-in" step="0.1" min="0" style="width:65px; background:var(--card); color:var(--fg); border:1px solid var(--line); border-radius:4px; padding:3px 6px; font-family:monospace; font-size:12px;">
              </div>
              <div class="field" style="margin:0;">
                <label class="field-label" style="margin:0; font-size:11px;">Trim Out (s):</label>
                <input type="number" id="pc-trim-out" step="0.1" min="0" style="width:65px; background:var(--card); color:var(--fg); border:1px solid var(--line); border-radius:4px; padding:3px 6px; font-family:monospace; font-size:12px;">
              </div>
            </div>

            <!-- Speed & Volume Sliders -->
            <div>
              <div style="display:flex; justify-content:space-between; align-items:center; font-size:11px;">
                <span>Playback Speed</span>
                <span id="pc-speed-label" class="mono" style="color:var(--accent);">1.0x</span>
              </div>
              <input type="range" id="pc-speed-slider" class="ig-vol-slider" min="0.25" max="3.0" step="0.25" value="1.0" style="margin-top:4px;">
            </div>

            <div>
              <div style="display:flex; justify-content:space-between; align-items:center; font-size:11px;">
                <span>Volume & Mute</span>
                <span id="pc-volume-label" class="mono" style="color:var(--accent);">100%</span>
              </div>
              <div class="ig-vol-row" style="margin-top:4px;">
                <input type="range" id="pc-volume-slider" class="ig-vol-slider" min="0" max="200" value="100">
                <button type="button" class="ig-mute-btn" id="pc-mute-btn">🔊</button>
              </div>
            </div>

            <!-- Stickers & Visual Effects -->
            <div>
              <span class="field-label" style="display:block; margin-bottom:4px; font-size:11px;">Visual Effect:</span>
              <div class="ig-pill-group" id="pc-effect-pills">
                <button type="button" class="ig-pill active" data-effect="clean">✨ Clean</button>
                <button type="button" class="ig-pill" data-effect="vhs">📼 VHS</button>
                <button type="button" class="ig-pill" data-effect="film_grain">🎞️ Film</button>
                <button type="button" class="ig-pill" data-effect="soft_glow">💫 Glow</button>
                <button type="button" class="ig-pill" data-effect="teal_orange">🎬 Teal/Orange</button>
                <button type="button" class="ig-pill" data-effect="bw_noir">🖤 B&W</button>
              </div>
            </div>

            <div>
              <span class="field-label" style="display:block; margin-bottom:4px; font-size:11px;">Overlay Sticker:</span>
              <div class="ig-pill-group" id="pc-sticker-pills">
                <button type="button" class="ig-pill active" data-sticker="">None</button>
                <button type="button" class="ig-pill" data-sticker="🔥 MUST WATCH">🔥 MUST WATCH</button>
                <button type="button" class="ig-pill" data-sticker="💡 LIFE HACK">💡 LIFE HACK</button>
                <button type="button" class="ig-pill" data-sticker="🚨 BREAKING">🚨 BREAKING</button>
                <button type="button" class="ig-pill" data-sticker="🤫 TOP SECRET">🤫 TOP SECRET</button>
                <button type="button" class="ig-pill" data-sticker="⚠️ PART 1">⚠️ PART 1</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Bottom Layout: Interactive Multi-Track Timeline -->
        <div style="background:var(--well); border:1px solid var(--line); border-radius:10px; padding:12px; overflow-x:auto;" id="pc-timeline-scroll">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <div style="display:flex; align-items:center; gap:8px;">
              <span style="font-size:11px; font-weight:700; text-transform:uppercase; color:var(--brand);">Tracks</span>
              <span id="pc-timeline-duration" class="mono" style="font-size:11px; color:var(--muted);">0.0s</span>
            </div>
            <div style="display:flex; align-items:center; gap:6px;">
              <button type="button" class="btn btn-icon" id="pc-zoom-out" style="padding:2px 8px; font-size:11px;">🔍 -</button>
              <button type="button" class="btn btn-icon" id="pc-zoom-in" style="padding:2px 8px; font-size:11px;">🔍 +</button>
            </div>
          </div>

          <!-- Multi-Track Canvas & Clips Container -->
          <div id="pc-tracks-container" style="display:flex; flex-direction:column; gap:6px; min-width:400px; position:relative;">
            <!-- Rendered by _renderTimelineTracks -->
          </div>
        </div>
      </div>
    `;

    // Connect compositor to the viewport canvas
    const canvas = document.getElementById('pc-viewport-canvas');
    if (canvas) {
      this.compositor.canvas = canvas;
      this.compositor.ctx = canvas.getContext('2d');
    }
  }

  _bindEvents() {
    // Play/Pause button
    const playBtn = document.getElementById('pc-play-btn');
    if (playBtn) {
      playBtn.onclick = () => {
        if (this.clock.isPlaying) {
          this.clock.pause();
        } else {
          this.clock.play();
        }
      };
    }

    // Split button
    const splitBtn = document.getElementById('pc-split-btn');
    if (splitBtn) {
      splitBtn.onclick = () => {
        const curTime = this.clock.getCurrentTime();
        if (this.selectedClipId) {
          this.state.splitClip(this.selectedClipId, curTime);
        } else {
          // Split active video clip at playhead
          const active = this.state.getActiveClipsAt(curTime).find(i => i.track.type === 'video');
          if (active) {
            this.state.splitClip(active.clip.id, curTime);
          }
        }
      };
    }

    // Undo / Redo
    const undoBtn = document.getElementById('pc-undo-btn');
    if (undoBtn) undoBtn.onclick = () => this.state.undo();
    const redoBtn = document.getElementById('pc-redo-btn');
    if (redoBtn) redoBtn.onclick = () => this.state.redo();

    // Ratio selector
    const ratioPills = document.querySelectorAll('#pc-ratio-pills .ig-pill');
    ratioPills.forEach(p => {
      p.onclick = () => {
        ratioPills.forEach(x => x.classList.remove('active'));
        p.classList.add('active');
        this.state.setAspectRatio(p.dataset.ratio);
        const ov = document.getElementById('pc-ratio-overlay');
        if (ov) ov.textContent = p.dataset.ratio;
      };
    });

    // Speed slider
    const speedSlider = document.getElementById('pc-speed-slider');
    if (speedSlider) {
      speedSlider.oninput = () => {
        const val = parseFloat(speedSlider.value);
        document.getElementById('pc-speed-label').textContent = val.toFixed(2) + 'x';
        if (this.selectedClipId) {
          this.state.setClipSpeed(this.selectedClipId, val);
        }
      };
    }

    // Volume slider & mute
    const volSlider = document.getElementById('pc-volume-slider');
    if (volSlider) {
      volSlider.oninput = () => {
        const val = parseInt(volSlider.value, 10);
        document.getElementById('pc-volume-label').textContent = val + '%';
        if (this.selectedClipId) {
          this.state.setClipVolume(this.selectedClipId, val / 100);
        }
      };
    }

    // AI Captions button
    const aiCapsBtn = document.getElementById('pc-auto-captions-btn');
    if (aiCapsBtn) {
      aiCapsBtn.onclick = async () => {
        aiCapsBtn.disabled = true;
        aiCapsBtn.innerHTML = '<span class="spinner"></span> Transcribing...';
        try {
          const mainVideoTrack = this.state.getTrackByType('video');
          if (!mainVideoTrack || !mainVideoTrack.clips.length) {
            alert('Please add a video clip first to transcribe.');
            return;
          }
          const firstClip = mainVideoTrack.clips[0];
          const asset = this.state.getAsset(firstClip.asset_id);
          if (!asset || !asset.url) return;

          const res = await fetch(asset.url);
          const blob = await res.blob();
          const transcript = await this.transcriber.transcribe(blob, (status, pct) => {
            aiCapsBtn.textContent = `AI: ${Math.round(pct * 100)}%`;
          });

          if (transcript && transcript.words && transcript.words.length) {
            const subTrack = this.state.getTrackByType('subtitle') || this.state.addTrack({ type: 'subtitle', name: 'Captions' });
            subTrack.clips = [];
            this.state.addClip(subTrack.id, {
              start: firstClip.start,
              end: firstClip.end,
              captions: {
                style: 'hormozi',
                words: transcript.words,
              },
            });
          }
        } catch (err) {
          console.error('[EditorUI] AI transcribe error:', err);
        } finally {
          aiCapsBtn.disabled = false;
          aiCapsBtn.textContent = '✨ AI Captions';
        }
      };
    }

    // Export button
    const exportBtn = document.getElementById('pc-export-btn');
    if (exportBtn) {
      exportBtn.onclick = async () => {
        exportBtn.disabled = true;
        exportBtn.innerHTML = '<span class="spinner"></span> Exporting...';
        try {
          const blob = await this.exporter.export({ quality: '1080p_standard' }, (msg, pct) => {
            exportBtn.textContent = `Export ${Math.round(pct * 100)}%`;
          });
          this.exporter.downloadBlob(blob, `${this.state.project.title || 'PulseCut'}.mp4`);
        } catch (err) {
          alert('Export error: ' + err.message);
        } finally {
          exportBtn.disabled = false;
          exportBtn.textContent = '⬇️ Export MP4';
        }
      };
    }

    // Zoom timeline
    const zoomIn = document.getElementById('pc-zoom-in');
    if (zoomIn) zoomIn.onclick = () => { this.zoomLevel = Math.min(200, this.zoomLevel + 20); this._renderTimelineTracks(); };
    const zoomOut = document.getElementById('pc-zoom-out');
    if (zoomOut) zoomOut.onclick = () => { this.zoomLevel = Math.max(20, this.zoomLevel - 20); this._renderTimelineTracks(); };
  }

  _setupStateSubscribers() {
    this.state.subscribe(() => {
      this._renderTimelineTracks();
      const dur = this.state.project.canvas.duration || 0;
      this.clock.setDuration(dur);
      const durEl = document.getElementById('pc-timeline-duration');
      if (durEl) durEl.textContent = dur.toFixed(1) + 's';
    });

    this.clock.subscribe((time, event) => {
      const timeEl = document.getElementById('pc-time-overlay');
      if (timeEl) {
        const m = Math.floor(time / 60);
        const s = (time % 60).toFixed(1);
        timeEl.textContent = `${m}:${s < 10 ? '0' : ''}${s}`;
      }
      const icon = document.getElementById('pc-play-icon');
      if (icon) {
        icon.innerHTML = this.clock.isPlaying
          ? '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>'
          : '<polygon points="5 3 19 12 5 21 5 3"/>';
      }
      this._updatePlayheadPosition(time);
    });
  }

  _renderTimelineTracks() {
    const container = document.getElementById('pc-tracks-container');
    if (!container) return;

    container.innerHTML = '';
    const dur = Math.max(5.0, this.state.project.canvas.duration || 10.0);
    const totalW = dur * this.zoomLevel;

    // Track rows
    this.state.tracks.forEach(track => {
      const row = document.createElement('div');
      row.style = `position:relative; height:44px; background:var(--card); border:1px solid var(--line); border-radius:6px; width:${totalW}px; margin-bottom:4px; display:flex; align-items:center;`;

      track.clips.forEach(clip => {
        const clipW = (clip.end - clip.start) * this.zoomLevel;
        const clipL = clip.start * this.zoomLevel;
        const isSelected = clip.id === this.selectedClipId;

        const clipEl = document.createElement('div');
        clipEl.style = `position:absolute; left:${clipL}px; width:${clipW}px; height:34px; background:${track.type === 'video' ? '#3b82f6' : track.type === 'audio' ? '#10b981' : '#f59e0b'}; color:#fff; border-radius:4px; border:${isSelected ? '2px solid #fff' : '1px solid rgba(0,0,0,0.3)'}; font-size:10px; font-weight:700; padding:2px 6px; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; display:flex; align-items:center; cursor:pointer; user-select:none; box-sizing:border-box;`;
        clipEl.textContent = `${track.name}: ${(clip.end - clip.start).toFixed(1)}s`;

        clipEl.onclick = (e) => {
          e.stopPropagation();
          this.selectedClipId = clip.id;
          this._updateInspector(clip, track);
          this._renderTimelineTracks();
        };

        row.appendChild(clipEl);
      });

      container.appendChild(row);
    });

    // Playhead line
    let playhead = document.getElementById('pc-playhead-line');
    if (!playhead) {
      playhead = document.createElement('div');
      playhead.id = 'pc-playhead-line';
      playhead.style = 'position:absolute; top:0; bottom:0; width:2px; background:#ef4444; z-index:10; pointer-events:none;';
      container.appendChild(playhead);
    }
  }

  _updatePlayheadPosition(time) {
    const playhead = document.getElementById('pc-playhead-line');
    if (playhead) {
      playhead.style.left = `${time * this.zoomLevel}px`;
    }
  }

  _updateInspector(clip, track) {
    const label = document.getElementById('pc-selection-label');
    if (label) label.textContent = `${track.name} (Clip: ${clip.id.slice(0, 8)})`;

    const trimIn = document.getElementById('pc-trim-in');
    if (trimIn) trimIn.value = clip.trim_in.toFixed(1);
    const trimOut = document.getElementById('pc-trim-out');
    if (trimOut) trimOut.value = clip.trim_out.toFixed(1);

    const speedSlider = document.getElementById('pc-speed-slider');
    if (speedSlider) speedSlider.value = clip.speed || 1.0;
    const speedLabel = document.getElementById('pc-speed-label');
    if (speedLabel) speedLabel.textContent = (clip.speed || 1.0).toFixed(2) + 'x';

    const volSlider = document.getElementById('pc-volume-slider');
    if (volSlider) volSlider.value = Math.round((clip.volume || 1.0) * 100);
    const volLabel = document.getElementById('pc-volume-label');
    if (volLabel) volLabel.textContent = Math.round((clip.volume || 1.0) * 100) + '%';
  }
}

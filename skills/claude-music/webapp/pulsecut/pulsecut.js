/**
 * PulseCut: Web Studio Orchestrator
 * Connects Timeline, Audio Clock, Canvas Compositor, Whisper AI, and Exporter.
 * 100% Free, Client-Side, Zero Paid Dependencies
 */

import { TimelineState } from './timeline-state.js';
import { MediaImporter } from './media-importer.js';
import { AudioClock } from './audio-clock.js';
import { Compositor } from './compositor.js';
import { SpeechTranscriber } from './speech-transcriber.js';
import { ExportEngine } from './export-engine.js';
import { StorageManager } from './storage-manager.js';
import { EditorUI } from './editor-ui.js';

export class PulseCutApp {
  constructor() {
    this.state = new TimelineState();
    this.clock = new AudioClock();
    this.importer = new MediaImporter();
    this.storage = new StorageManager();
    this.transcriber = new SpeechTranscriber();

    this.compositor = null;
    this.exporter = null;
    this.ui = null;
  }

  mount(containerEl) {
    if (typeof containerEl === 'string') {
      containerEl = document.querySelector(containerEl);
    }
    if (!containerEl) throw new Error('PulseCut container element not found');

    // Create temporary canvas for compositor initialization
    const initialCanvas = document.createElement('canvas');
    initialCanvas.width = 1080;
    initialCanvas.height = 1920;

    this.compositor = new Compositor(initialCanvas, this.state, this.clock);
    this.exporter = new ExportEngine(this.compositor, this.state);
    this.ui = new EditorUI(
      containerEl,
      this.state,
      this.clock,
      this.compositor,
      this.exporter,
      this.transcriber
    );

    const viewportCanvas = containerEl.querySelector('#pc-viewport-canvas');
    if (viewportCanvas) {
      this.compositor.attachCanvas(viewportCanvas);
    }

    this.compositor.start();
    this._setupAutosave();
  }

  async loadFromProjectJson(jsonOrObj) {
    this.state.deserialize(jsonOrObj, true);
    if (this.ui) {
      this.ui._renderTimelineTracks();
    }
  }

  async importMediaFile(file, trackType = 'video') {
    const asset = await this.importer.importFile(file);
    this.state.addAsset(asset);

    const track = this.state.getTrackByType(trackType) || this.state.tracks[0];
    const curTime = this.clock.getCurrentTime();

    const clip = this.state.addClip(track.id, {
      asset_id: asset.id,
      start: curTime,
      end: curTime + Math.min(30.0, asset.duration),
      trim_in: 0,
      trim_out: Math.min(30.0, asset.duration),
    });

    return { asset, clip };
  }

  _setupAutosave() {
    let timer = null;
    this.state.subscribe(() => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        try {
          await this.storage.saveProject(this.state);
        } catch (e) {
          console.warn('[PulseCut] autosave error:', e);
        }
      }, 1000);
    });
  }
}

// Global initialization helper for webapp integration
window.PulseCut = {
  createInstance: () => new PulseCutApp(),
};

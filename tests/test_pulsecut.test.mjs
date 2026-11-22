/**
 * Native Node.js Test Suite for PulseCut Client-Side Engine
 * Runs natively in Node 18+ via: node --test tests/test_pulsecut.test.mjs
 * Zero external npm dependencies.
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { TimelineState } from '../skills/claude-music/webapp/pulsecut/timeline-state.js';
import { SpeechTranscriber } from '../skills/claude-music/webapp/pulsecut/speech-transcriber.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

describe('PulseCut TimelineState Engine', () => {
  test('creates a default 9:16 project with 4 standard tracks', () => {
    const state = new TimelineState();
    assert.ok(state.project.id.startsWith('proj_'));
    assert.equal(state.project.canvas.width, 1080);
    assert.equal(state.project.canvas.height, 1920);
    assert.equal(state.project.canvas.aspect_ratio, '9:16');
    assert.equal(state.project.canvas.fps, 30);

    assert.equal(state.tracks.length, 4);
    assert.equal(state.tracks[0].type, 'video');
    assert.equal(state.tracks[1].type, 'sticker');
    assert.equal(state.tracks[2].type, 'subtitle');
    assert.equal(state.tracks[3].type, 'audio');
  });

  test('asset registration and retrieval', () => {
    const state = new TimelineState();
    const asset = state.addAsset({
      name: 'sample_reel.mp4',
      type: 'video',
      duration: 18.5,
      width: 1080,
      height: 1920,
      source: 'blob:http://localhost/1234',
    });

    assert.ok(asset.id.startsWith('asset_'));
    assert.equal(state.assets.length, 1);
    assert.equal(state.getAsset(asset.id).name, 'sample_reel.mp4');

    state.removeAsset(asset.id);
    assert.equal(state.assets.length, 0);
    assert.equal(state.getAsset(asset.id), null);
  });

  test('clip manipulation, trimming, and duration calculation', () => {
    const state = new TimelineState();
    const videoTrack = state.tracks[0];

    const clip1 = state.addClip(videoTrack.id, {
      name: 'Clip 1',
      start: 0,
      end: 5.0,
      trim_in: 0,
      trim_out: 5.0,
      volume: 1.0,
      speed: 1.0,
    });
    assert.ok(clip1.id.startsWith('clip_'));
    assert.equal(videoTrack.clips.length, 1);

    const clip2 = state.addClip(videoTrack.id, {
      name: 'Clip 2',
      start: 5.0,
      end: 12.0,
      trim_in: 0,
      trim_out: 7.0,
      volume: 0.8,
      speed: 1.0,
    });

    assert.equal(videoTrack.clips.length, 2);
    assert.equal(state.project.canvas.duration, 12.0);

    // Update clip trim
    state.updateClip(clip1.id, { end: 6.0, trim_out: 6.0 });
    const updated = state.getClip(clip1.id).clip;
    assert.equal(updated.end, 6.0);

    // Remove clip
    state.removeClip(clip2.id);
    assert.equal(videoTrack.clips.length, 1);
    assert.equal(state.project.canvas.duration, 6.0);
  });

  test('getActiveClipsAt returns accurate time slices', () => {
    const state = new TimelineState();
    const videoTrack = state.tracks[0];
    const audioTrack = state.tracks[3];

    state.addClip(videoTrack.id, { id: 'v1', start: 0.0, end: 4.0 });
    state.addClip(videoTrack.id, { id: 'v2', start: 4.0, end: 10.0 });
    state.addClip(audioTrack.id, { id: 'a1', start: 0.0, end: 10.0 });

    // At t = 2.0s: v1 and a1 should be active
    const activeAt2 = state.getActiveClipsAt(2.0);
    assert.equal(activeAt2.length, 2);
    const idsAt2 = activeAt2.map(item => item.clip.id).sort();
    assert.deepEqual(idsAt2, ['a1', 'v1']);

    // At t = 5.0s: v2 and a1 should be active
    const activeAt5 = state.getActiveClipsAt(5.0);
    assert.equal(activeAt5.length, 2);
    const idsAt5 = activeAt5.map(item => item.clip.id).sort();
    assert.deepEqual(idsAt5, ['a1', 'v2']);

    // At t = 11.0s: no clips active
    const activeAt11 = state.getActiveClipsAt(11.0);
    assert.equal(activeAt11.length, 0);
  });

  test('undo and redo history maintains state consistency', () => {
    const state = new TimelineState();
    const videoTrack = state.tracks[0];

    assert.equal(state.canUndo(), false);
    state.addClip(videoTrack.id, { id: 'clip_undo_test', start: 0, end: 5.0 });
    assert.equal(state.canUndo(), true);
    assert.equal(videoTrack.clips.length, 1);

    // Undo
    const undone = state.undo();
    assert.ok(undone);
    assert.equal(state.tracks[0].clips.length, 0);
    assert.equal(state.canRedo(), true);

    // Redo
    const redone = state.redo();
    assert.ok(redone);
    assert.equal(state.tracks[0].clips.length, 1);
    assert.equal(state.tracks[0].clips[0].id, 'clip_undo_test');
  });

  test('serialization and schema compliance with docs/timeline-schema.json', () => {
    const state = new TimelineState();
    const asset = state.addAsset({
      name: 'video.mp4',
      type: 'video',
      duration: 15.0,
      width: 1080,
      height: 1920,
    });
    state.addClip(state.tracks[0].id, {
      asset_id: asset.id,
      name: 'Main Video',
      start: 0,
      end: 15.0,
      trim_in: 0,
      trim_out: 15.0,
    });

    const serialized = state.serialize();
    const parsed = JSON.parse(serialized);

    // Verify top-level structure required by docs/timeline-schema.json
    assert.ok(parsed.project);
    assert.ok(parsed.assets);
    assert.ok(parsed.tracks);

    // Check project canvas
    assert.equal(parsed.project.canvas.width, 1080);
    assert.equal(parsed.project.canvas.height, 1920);
    assert.equal(parsed.project.canvas.aspect_ratio, '9:16');
    assert.equal(parsed.project.canvas.duration, 15.0);

    // Verify round-trip deserialization
    const state2 = new TimelineState();
    state2.deserialize(serialized);
    assert.equal(state2.tracks[0].clips.length, 1);
    assert.equal(state2.assets.length, 1);
    assert.equal(state2.project.canvas.duration, 15.0);
  });
});

describe('PulseCut SpeechTranscriber Engine', () => {
  test('offline cadence segmentation produces timed caption words', () => {
    const transcriber = new SpeechTranscriber();
    const sampleText = 'The secret to building great reels is pacing and storytelling.';
    const totalDuration = 4.0;

    const words = transcriber.segmentSpeechCadence(sampleText, totalDuration);
    assert.ok(words.length > 0);
    assert.equal(words[0].word, 'THE');
    assert.equal(words[0].start, 0);
    assert.ok(words[words.length - 1].end <= totalDuration + 0.1);

    for (let i = 0; i < words.length - 1; i++) {
      assert.ok(words[i].start <= words[i].end, `Word ${i} start <= end`);
      assert.ok(words[i].end <= words[i + 1].start + 0.05, `Word ${i} precedes word ${i+1}`);
    }
  });
});

#!/usr/bin/env node
/*
 * Phase 0 実証: MIDI的なスコア → SN76489のVGM → 既存ネイティブヘルパ → WAV。
 *
 * 「任意の音源チップで鳴らす」機能（CHIP_VOICE_PLAN_ja.md）の採用方式が、
 * ネイティブ側の変更ゼロで成立するかを判定するための使い捨てのspike。
 * 本実装ではないので、ここでの設計判断（チャンネル固定割り当て、単一チップ、
 * 音色データ無し）はPhase 1で作り直す前提。
 *
 * 検証するのは4点:
 *   A 生成   固定スコアからVGMコマンド列を組み立てられるか
 *   B 往復   生成したVGMを既存のVGM→MIDIデコーダが元のノートとして復元できるか
 *            （Phase 1のテスト戦略「既存デコーダをオラクルにする」の成立確認）
 *   C 描画   vgm2midi_stems の既存モードがそのVGMを正確な尺のWAVにできるか
 *   D 音高   描画結果の実測周波数がスコアのノートと一致するか（音程変換の検算）
 *   E コスト 実曲の長さでのレンダリング時間 = 編集のたびに払うコスト
 *
 * C/D/E はネイティブヘルパ（macOS universal binary）が要る。それ以外の環境では
 * A/B だけを実行してスキップする（--require-render で失敗扱いにできる）。
 *
 *   node scripts/spike-chip-synth.js
 *   node scripts/spike-chip-synth.js --seconds 180 --keep /tmp/spike
 */

'use strict';

const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const SAMPLE_RATE = 44100;
const SN76489_CLOCK = 3579545;
// SN76489の分周比。VGMヘッダ0x28のフラグbit3が未設定のときの既定値で、
// midi-math.ts の psgRegisterToFrequency() が使う値と同じ。
const SN76489_DIVISOR = 32;
const SN76489_MAX_PERIOD = 0x3FF;
const TONE_CHANNELS = 3;
const NOISE_CHANNEL = 3;
const DEVICE_TYPE_SN76489 = 0x00; // midi-converter.ts の track-metadata と同じ値
const DEFAULT_HELPER = path.join(__dirname, '..', 'native', 'bin', 'vgm2midi_stems');

// ---------------------------------------------------------------------------
// 音程・音量の変換（Phase 1で chips/ 配下へ移す想定の中核ロジック）
// ---------------------------------------------------------------------------

/** MIDIノート番号を周波数[Hz]へ。 */
function noteToFrequency(note) {
  return 440 * Math.pow(2, (note - 69) / 12);
}

/**
 * MIDIノート番号をSN76489の10bit周期レジスタ値へ。
 * midi-math.ts の psgRegisterToFrequency()（period → Hz）の逆関数。
 * 音域外は端へクランプし、クランプしたことを呼び出し元へ伝える。
 */
function noteToPeriod(note, clock = SN76489_CLOCK) {
  const exact = clock / (SN76489_DIVISOR * noteToFrequency(note));
  const period = Math.max(1, Math.min(SN76489_MAX_PERIOD, Math.round(exact)));
  return { period, clamped: Math.round(exact) !== period };
}

/** 周期レジスタ値から実際に鳴る周波数[Hz]（丸め後の真値）。 */
function periodToFrequency(period, clock = SN76489_CLOCK) {
  return clock / (SN76489_DIVISOR * period);
}

/** 2つの周波数の隔たりをセントで返す。 */
function centsBetween(actualHz, expectedHz) {
  return 1200 * Math.log2(actualHz / expectedHz);
}

/**
 * ベロシティ(1..127)をSN76489の4bit減衰値(0=最大音量, 15=無音)へ。
 * 1ステップ2dBなので、フルスケールからの相対dBを2で割って丸める。
 */
function velocityToAttenuation(velocity) {
  if (velocity <= 0) return 15;
  const decibels = 20 * Math.log10(Math.min(127, velocity) / 127);
  return Math.max(0, Math.min(15, Math.round(-decibels / 2)));
}

/** この構成のSN76489が鳴らせる最低ノート番号（周期レジスタ幅で決まる）。 */
function lowestPlayableNote(clock = SN76489_CLOCK) {
  const lowestHz = clock / (SN76489_DIVISOR * SN76489_MAX_PERIOD);
  return Math.ceil(69 + 12 * Math.log2(lowestHz / 440));
}

// ---------------------------------------------------------------------------
// 固定スコア
// ---------------------------------------------------------------------------

/**
 * 4小節・120BPMのイ短調フレーズ。Phase 1のスコアJSONの原型で、
 * 時刻は秒の絶対値（速度・移調は適用済みという前提も同じ）。
 * トーン3声はSN76489の物理チャンネルへ1:1で固定する — ボイス割り当ては
 * Phase 1の仕事なので、ここでは意図的に持ち込まない。
 */
function buildPhrase() {
  const beat = 0.5; // 120 BPM
  const melodyPitches = [69, 72, 76, 72, 77, 76, 72, 69, 71, 74, 79, 74, 76, 72, 69, 69];
  const melody = melodyPitches.map((note, index) => ({
    t: index * beat * 0.5,
    d: beat * 0.45,
    n: note,
    v: index % 4 === 0 ? 112 : 88,
  }));
  const harmony = [64, 65, 62, 64].map((note, index) => ({
    t: index * beat * 2,
    d: beat * 1.9,
    n: note,
    v: 72,
  }));
  const bass = [45, 53, 50, 45, 47, 55, 52, 45].map((note, index) => ({
    t: index * beat,
    d: beat * 0.9,
    n: note,
    v: 100,
  }));
  // ノイズは音高ではなくレート(0=最速/高域, 1=中域, 2=低域)で3種を叩き分ける。
  const drums = [];
  for (let step = 0; step < 16; step++) {
    drums.push({ t: step * beat * 0.5, d: 0.05, rate: 0, v: step % 2 === 0 ? 70 : 50 }); // hat
    if (step % 8 === 0) drums.push({ t: step * beat * 0.5, d: 0.12, rate: 2, v: 120 });  // kick
    if (step % 8 === 4) drums.push({ t: step * beat * 0.5, d: 0.10, rate: 1, v: 105 });  // snare
  }
  return {
    lengthSeconds: 16 * beat * 0.5,
    parts: [
      { name: 'melody', channel: 0, notes: melody },
      { name: 'harmony', channel: 1, notes: harmony },
      { name: 'bass', channel: 2, notes: bass },
      { name: 'drums', channel: NOISE_CHANNEL, notes: drums },
    ],
  };
}

/** フレーズを目標秒数まで繰り返して1本のスコアにする（コスト計測用）。 */
function repeatPhrase(phrase, targetSeconds) {
  const repeats = Math.max(1, Math.ceil(targetSeconds / phrase.lengthSeconds));
  const parts = phrase.parts.map((part) => ({ ...part, notes: [] }));
  for (let index = 0; index < repeats; index++) {
    const offset = index * phrase.lengthSeconds;
    phrase.parts.forEach((part, partIndex) => {
      for (const note of part.notes) parts[partIndex].notes.push({ ...note, t: note.t + offset });
    });
  }
  return { lengthSeconds: repeats * phrase.lengthSeconds, parts };
}

// ---------------------------------------------------------------------------
// スコア → VGM
// ---------------------------------------------------------------------------

/** スコアをサンプル位置つきのレジスタ書き込み列へ展開する。 */
function scoreToWrites(score) {
  const writes = [];
  const clamps = [];
  const at = (seconds) => Math.round(seconds * SAMPLE_RATE);
  const push = (sample, byte, channel, kind) => writes.push({ sample, byte, channel, kind });

  for (const part of score.parts) {
    for (const note of part.notes) {
      const attenuation = velocityToAttenuation(note.v);
      if (part.channel === NOISE_CHANNEL) {
        // ノイズ: 制御レジスタ(白色ノイズ + レート)を書いてから音量を開ける。
        push(at(note.t), 0xE0 | 0x04 | (note.rate & 0x03), part.channel, 'setup');
        push(at(note.t), 0xF0 | attenuation, part.channel, 'on');
        push(at(note.t + note.d), 0xF0 | 0x0F, part.channel, 'off');
        continue;
      }
      const { period, clamped } = noteToPeriod(note.n);
      if (clamped) clamps.push({ part: part.name, note: note.n, t: note.t });
      push(at(note.t), 0x80 | (part.channel << 5) | (period & 0x0F), part.channel, 'setup');
      push(at(note.t), (period >> 4) & 0x3F, part.channel, 'setup');
      push(at(note.t), 0x90 | (part.channel << 5) | attenuation, part.channel, 'on');
      push(at(note.t + note.d), 0x90 | (part.channel << 5) | 0x0F, part.channel, 'off');
    }
  }
  // 同一サンプルに集まった書き込みは投入順を保つ（周期のlatch→dataが割れないように）。
  return { writes: writes.map((write, order) => ({ ...write, order })).sort((left, right) =>
    left.sample - right.sample || left.order - right.order), clamps };
}

/**
 * チャンネルごとに、発音と消音の書き込みが1:1で対応しているか数える。
 *
 * 往復デコードだけでは消音漏れを検知できない — デコーダはノイズの制御レジスタ
 * 書き込みごとに打点を作るので、消音を丸ごと落としても復元される打点数は
 * 変わらない（実際にmutation testで素通りした）。鳴りっぱなしは耳には一発で
 * 分かるが自動検証には映らないので、生成側の構造不変条件として別途見る。
 */
function measureWriteBalance(writes) {
  const balance = {};
  for (const write of writes) {
    if (write.kind === 'setup') continue;
    const entry = balance[write.channel] || (balance[write.channel] = { on: 0, off: 0 });
    entry[write.kind]++;
  }
  return balance;
}

/** 待機サンプル数を 0x61 コマンド列として積む。 */
function pushWait(bytes, samples) {
  let remaining = samples;
  while (remaining > 0) {
    const chunk = Math.min(0xFFFF, remaining);
    bytes.push(0x61, chunk & 0xFF, (chunk >> 8) & 0xFF);
    remaining -= chunk;
  }
}

/**
 * スコアからVGM 1.61のバイト列を組み立てる。
 * ヘッダのオフセットは vgm-parser.ts が読んでいる位置と同じ
 * （0x0C=SN76489クロック、0x18=総サンプル数、0x34=データ開始）。
 */
function synthesizeVgm(score) {
  const { writes, clamps } = scoreToWrites(score);
  const totalSamples = Math.round(score.lengthSeconds * SAMPLE_RATE) + SAMPLE_RATE; // 余韻1秒

  const commands = [];
  // 全チャンネルを無音(減衰15)に初期化してから始める。
  for (let channel = 0; channel < 4; channel++) commands.push(0x50, 0x90 | (channel << 5) | 0x0F);
  let cursor = 0;
  for (const write of writes) {
    if (write.sample > cursor) {
      pushWait(commands, write.sample - cursor);
      cursor = write.sample;
    }
    commands.push(0x50, write.byte);
  }
  if (totalSamples > cursor) pushWait(commands, totalSamples - cursor);
  commands.push(0x66); // end of sound data

  const dataOffset = 0x100;
  const buffer = Buffer.alloc(dataOffset + commands.length);
  buffer.write('Vgm ', 0, 'ascii');
  buffer.writeUInt32LE(buffer.length - 4, 0x04);   // EOF offset
  buffer.writeUInt32LE(0x00000161, 0x08);          // version 1.61
  buffer.writeUInt32LE(SN76489_CLOCK, 0x0C);       // SN76489 clock
  buffer.writeUInt32LE(totalSamples, 0x18);        // total samples
  buffer.writeUInt32LE(0, 0x1C);                   // loop offset: ループ無し
  buffer.writeUInt32LE(0, 0x20);                   // loop samples
  buffer.writeUInt32LE(dataOffset - 0x34, 0x34);   // VGM data offset
  Buffer.from(commands).copy(buffer, dataOffset);

  return {
    buffer,
    totalSamples,
    commandCount: writes.length,
    clamps,
    writeBalance: measureWriteBalance(writes),
  };
}

// ---------------------------------------------------------------------------
// WAV計測
// ---------------------------------------------------------------------------

function readWavSamples(file) {
  const wav = fs.readFileSync(file);
  assert.equal(wav.toString('ascii', 0, 4), 'RIFF', `${file} is not a RIFF file`);
  return new Int16Array(wav.buffer, wav.byteOffset + 44, (wav.length - 44) / 2);
}

function readWavFrames(file) {
  return (fs.statSync(file).size - 44) / 4;
}

function measurePeak(samples) {
  let peak = 0;
  for (const value of samples) peak = Math.max(peak, Math.abs(value));
  return peak;
}

/**
 * stereo S16の左chについて、指定フレーム範囲のゼロクロスから基本周波数を推定する。
 * SN76489は矩形波なので、単一チャンネルだけを描画した音であれば十分に正確。
 */
function measureFrequency(samples, startFrame, frameCount) {
  let crossings = 0;
  let previous = samples[startFrame * 2];
  for (let frame = startFrame + 1; frame < startFrame + frameCount; frame++) {
    const value = samples[frame * 2];
    if ((previous < 0) !== (value < 0)) crossings++;
    previous = value;
  }
  return crossings / 2 / (frameCount / SAMPLE_RATE);
}

// ---------------------------------------------------------------------------
// 往復デコード用の最小SMFリーダ
// ---------------------------------------------------------------------------

/** MidiConverterが書いたSMFからノートオン列（トラック別・秒）を取り出す。 */
function readMidiNotes(file) {
  const buffer = fs.readFileSync(file);
  assert.equal(buffer.toString('ascii', 0, 4), 'MThd', 'not a standard MIDI file');
  const ticksPerBeat = buffer.readUInt16BE(12);
  let position = 8 + buffer.readUInt32BE(4);
  let microsecondsPerBeat = 500000;
  const tracks = [];

  while (position < buffer.length - 8) {
    assert.equal(buffer.toString('ascii', position, position + 4), 'MTrk', 'expected MTrk chunk');
    const length = buffer.readUInt32BE(position + 4);
    const end = position + 8 + length;
    let cursor = position + 8;
    let tick = 0;
    let status = 0;
    const notes = [];
    let name = '';

    while (cursor < end) {
      let delta = 0;
      for (;;) {
        const byte = buffer[cursor++];
        delta = (delta << 7) | (byte & 0x7F);
        if ((byte & 0x80) === 0) break;
      }
      tick += delta;

      if (buffer[cursor] & 0x80) status = buffer[cursor++];
      const kind = status & 0xF0;

      if (status === 0xFF) {
        const metaType = buffer[cursor++];
        let metaLength = 0;
        for (;;) {
          const byte = buffer[cursor++];
          metaLength = (metaLength << 7) | (byte & 0x7F);
          if ((byte & 0x80) === 0) break;
        }
        if (metaType === 0x03) name = buffer.toString('utf8', cursor, cursor + metaLength);
        if (metaType === 0x51) microsecondsPerBeat = (buffer[cursor] << 16) | (buffer[cursor + 1] << 8) | buffer[cursor + 2];
        cursor += metaLength;
      } else if (status === 0xF0 || status === 0xF7) {
        let sysexLength = 0;
        for (;;) {
          const byte = buffer[cursor++];
          sysexLength = (sysexLength << 7) | (byte & 0x7F);
          if ((byte & 0x80) === 0) break;
        }
        cursor += sysexLength;
      } else if (kind === 0x90) {
        const note = buffer[cursor++];
        const velocity = buffer[cursor++];
        if (velocity > 0) notes.push({ tick, note, velocity, channel: status & 0x0F });
      } else if (kind === 0x80 || kind === 0xA0 || kind === 0xB0 || kind === 0xE0) {
        cursor += 2;
      } else if (kind === 0xC0 || kind === 0xD0) {
        cursor += 1;
      } else {
        throw new Error(`unsupported MIDI status 0x${status.toString(16)} at ${cursor}`);
      }
    }

    const secondsPerTick = microsecondsPerBeat / 1_000_000 / ticksPerBeat;
    tracks.push({ name, notes: notes.map((note) => ({ ...note, t: note.tick * secondsPerTick })) });
    position = end;
  }
  return tracks;
}

// ---------------------------------------------------------------------------
// 検証ステップ
// ---------------------------------------------------------------------------

/** B: 生成VGMを既存デコーダに通し、スコアのノートが復元されるか確かめる。 */
function verifyRoundTrip(vgmPath, score, workDir) {
  const { VGMParser } = require('../dist/vgm-parser');
  const { MidiConverter } = require('../dist/midi-converter');
  const { prepareVGMPlayback } = require('../dist/vgm-playback');

  const parsed = new VGMParser(fs.readFileSync(vgmPath)).parse();
  const playback = prepareVGMPlayback(parsed, {});
  const converter = new MidiConverter(playback.data, { tempo: 120 });
  const midiPath = path.join(workDir, 'roundtrip.mid');
  converter.exportToFile(midiPath);

  const tracks = readMidiNotes(midiPath);
  const decoded = tracks.flatMap((track) => track.notes.map((note) => ({ ...note, track: track.name })));
  const melodic = decoded.filter((note) => note.channel !== 9);
  const percussive = decoded.filter((note) => note.channel === 9);

  const expectedTones = score.parts
    .filter((part) => part.channel !== NOISE_CHANNEL)
    .flatMap((part) => part.notes.map((note) => ({ ...note, part: part.name })));
  const expectedHits = score.parts
    .filter((part) => part.channel === NOISE_CHANNEL)
    .reduce((total, part) => total + part.notes.length, 0);

  const missing = expectedTones.filter((expected) => !melodic.some(
    (note) => note.note === expected.n && Math.abs(note.t - expected.t) <= 0.03,
  ));

  return {
    midiPath,
    decodedTracks: tracks.length,
    expectedToneNotes: expectedTones.length,
    decodedToneNotes: melodic.length,
    missingToneNotes: missing.length,
    missingExamples: missing.slice(0, 5),
    expectedNoiseHits: expectedHits,
    decodedPercussionNotes: percussive.length,
    percussionNotesUsed: [...new Set(percussive.map((note) => note.note))].sort((a, b) => a - b),
  };
}

/** ネイティブヘルパを1回呼び、所要時間[ms]を返す。 */
function runHelper(helper, argv) {
  const startedAt = process.hrtime.bigint();
  childProcess.execFileSync(helper, argv, { stdio: 'pipe' });
  return Number(process.hrtime.bigint() - startedAt) / 1e6;
}

/** C: 既存モードで、要求した尺ちょうどの可聴WAVが出るか。 */
function verifyRender(helper, vgmPath, totalSamples, workDir) {
  const mixDir = path.join(workDir, 'stems');
  fs.mkdirSync(mixDir, { recursive: true });
  const manifestPath = path.join(mixDir, 'spike.stems.json');
  const mixMs = runHelper(helper, [vgmPath, mixDir, String(totalSamples), manifestPath]);
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));

  assert.equal(manifest.sampleCount, totalSamples, 'manifest sample count must match the request');
  assert.equal(manifest.stems.length, 2, 'single-chip VGM must enumerate mix + exactly one device');
  const [mix, device] = manifest.stems;
  assert.equal(device.chip, 'SN76489', `expected SN76489 device stem, got ${device.chip}`);
  for (const stem of manifest.stems) {
    assert.equal(readWavFrames(stem.path), totalSamples, `${stem.path} must be exactly ${totalSamples} frames`);
  }
  const mixPeak = measurePeak(readWavSamples(mix.path));
  assert.ok(mixPeak > 1000, `synthesized VGM must be clearly audible, got peak=${mixPeak}`);

  // --selection: Phase 1 が実際に使う呼び出し形。トーンch0だけを取り出す。
  const channelPath = path.join(workDir, 'sn76489-ch0.wav');
  const selectionMs = runHelper(helper, [
    '--selection', vgmPath, channelPath, String(totalSamples),
    `${DEVICE_TYPE_SN76489}:0:1:0`,
  ]);
  assert.equal(readWavFrames(channelPath), totalSamples, 'selection render must match the requested length');
  const channelPeak = measurePeak(readWavSamples(channelPath));
  assert.ok(channelPeak > 1000, `selected channel must be audible, got peak=${channelPeak}`);
  assert.ok(channelPeak < mixPeak, 'one channel should be quieter than the full mix');

  return { mixPath: mix.path, channelPath, mixPeak, channelPeak, mixMs, selectionMs };
}

/** D: ch0だけを描画したWAVで、各ノートの実測周波数がスコアと合うか。 */
function verifyPitch(channelPath, score, toleranceCents) {
  const samples = readWavSamples(channelPath);
  const melody = score.parts.find((part) => part.channel === 0);
  const measurements = [];

  for (const note of melody.notes) {
    // アタック直後とリリース直前を避け、安定している中央部分だけを測る。
    const startFrame = Math.round((note.t + note.d * 0.25) * SAMPLE_RATE);
    const frameCount = Math.round(note.d * 0.5 * SAMPLE_RATE);
    if (frameCount < 256) continue;
    const measuredHz = measureFrequency(samples, startFrame, frameCount);
    const { period } = noteToPeriod(note.n);
    const targetHz = periodToFrequency(period);        // 丸め後にチップが出すはずの音
    const idealHz = noteToFrequency(note.n);           // 平均律の理論値
    measurements.push({
      note: note.n,
      measuredHz,
      targetHz,
      centsFromChip: centsBetween(measuredHz, targetHz),
      centsFromIdeal: centsBetween(measuredHz, idealHz),
    });
  }

  assert.ok(measurements.length > 0, 'pitch verification needs at least one measurable note');
  const worst = measurements.reduce(
    (accumulator, item) => (Math.abs(item.centsFromChip) > Math.abs(accumulator.centsFromChip) ? item : accumulator),
  );
  assert.ok(
    Math.abs(worst.centsFromChip) <= toleranceCents,
    `note ${worst.note}: measured ${worst.measuredHz.toFixed(1)}Hz vs chip target ${worst.targetHz.toFixed(1)}Hz `
    + `(${worst.centsFromChip.toFixed(1)} cents, tolerance ${toleranceCents})`,
  );

  const quantization = measurements.reduce(
    (accumulator, item) => Math.max(accumulator, Math.abs(item.centsFromIdeal)), 0,
  );
  return { measured: measurements.length, worstCentsFromChip: worst.centsFromChip, worstCentsFromIdeal: quantization };
}

/** E: 実曲の長さで描画し、実時間比を出す（編集のたびに払うコスト）。 */
function measureCost(helper, score, workDir, seconds) {
  const long = repeatPhrase(score, seconds);
  const synthStartedAt = process.hrtime.bigint();
  const synthesized = synthesizeVgm(long);
  const synthMs = Number(process.hrtime.bigint() - synthStartedAt) / 1e6;

  const vgmPath = path.join(workDir, 'cost.vgm');
  fs.writeFileSync(vgmPath, synthesized.buffer);
  const outputPath = path.join(workDir, 'cost.wav');
  const allChannelsMask = (1 << (TONE_CHANNELS + 1)) - 1; // トーン3 + ノイズ1
  const renderMs = runHelper(helper, [
    '--selection', vgmPath, outputPath, String(synthesized.totalSamples),
    `${DEVICE_TYPE_SN76489}:0:${allChannelsMask}:0`,
  ]);
  const audioSeconds = synthesized.totalSamples / SAMPLE_RATE;

  return {
    audioSeconds: Number(audioSeconds.toFixed(2)),
    noteCount: long.parts.reduce((total, part) => total + part.notes.length, 0),
    vgmBytes: synthesized.buffer.length,
    synthMs: Number(synthMs.toFixed(1)),
    renderMs: Number(renderMs.toFixed(1)),
    realtimeFactor: Number((audioSeconds / (renderMs / 1000)).toFixed(1)),
  };
}

// ---------------------------------------------------------------------------

function parseArgv(argv) {
  const options = { seconds: 180, keep: null, helper: process.env.VGM2MIDI_STEMS_HELPER || DEFAULT_HELPER, requireRender: false, toleranceCents: 12 };
  for (let index = 0; index < argv.length; index++) {
    const flag = argv[index];
    if (flag === '--seconds') options.seconds = Number(argv[++index]);
    else if (flag === '--keep') options.keep = argv[++index];
    else if (flag === '--helper') options.helper = argv[++index];
    else if (flag === '--tolerance-cents') options.toleranceCents = Number(argv[++index]);
    else if (flag === '--require-render') options.requireRender = true;
    else throw new Error(`unknown option: ${flag}`);
  }
  return options;
}

function main() {
  const options = parseArgv(process.argv.slice(2));
  const workDir = options.keep
    ? (fs.mkdirSync(options.keep, { recursive: true }), options.keep)
    : fs.mkdtempSync(path.join(os.tmpdir(), 'chip-synth-spike-'));

  // A: 生成
  const score = buildPhrase();
  const synthesized = synthesizeVgm(score);
  const vgmPath = path.join(workDir, 'spike.vgm');
  fs.writeFileSync(vgmPath, synthesized.buffer);

  const report = {
    workDir,
    chip: 'SN76489',
    generate: {
      scoreSeconds: score.lengthSeconds,
      noteCount: score.parts.reduce((total, part) => total + part.notes.length, 0),
      registerWrites: synthesized.commandCount,
      vgmBytes: synthesized.buffer.length,
      totalSamples: synthesized.totalSamples,
      outOfRangeNotes: synthesized.clamps.length,
      lowestPlayableNote: lowestPlayableNote(),
      writeBalance: synthesized.writeBalance,
    },
  };

  for (const [channel, entry] of Object.entries(synthesized.writeBalance)) {
    assert.equal(entry.off, entry.on,
      `channel ${channel}: ${entry.on} note-on writes but ${entry.off} note-off writes — voices would stay on`);
  }

  // B: 往復デコード（ネイティブ不要）
  report.roundTrip = verifyRoundTrip(vgmPath, score, workDir);
  assert.equal(report.roundTrip.missingToneNotes, 0,
    `round-trip lost ${report.roundTrip.missingToneNotes} tone note(s): `
    + JSON.stringify(report.roundTrip.missingExamples));
  assert.ok(report.roundTrip.decodedPercussionNotes > 0, 'noise hits should decode as GM percussion');

  // C/D/E: ネイティブヘルパが要る
  const helperUsable = fs.existsSync(options.helper) && process.platform === 'darwin';
  if (!helperUsable) {
    const reason = !fs.existsSync(options.helper)
      ? `native helper not found: ${options.helper}`
      : `native helper is a macOS binary; this platform is ${process.platform}`;
    if (options.requireRender) throw new Error(reason);
    report.render = { skipped: reason };
    report.verdict = 'PARTIAL — 生成と往復デコードのみ検証。描画・音高・コストはmacOSで再実行が必要';
  } else {
    report.render = verifyRender(options.helper, vgmPath, synthesized.totalSamples, workDir);
    report.pitch = verifyPitch(report.render.channelPath, score, options.toleranceCents);
    report.cost = measureCost(options.helper, score, workDir, options.seconds);
    report.verdict = 'PASS — ネイティブ変更ゼロで スコア→VGM→WAV が成立';
  }

  console.log(JSON.stringify(report, null, 2));
  if (report.cost) {
    console.log(
      `\n${report.cost.audioSeconds}秒の曲を ${report.cost.renderMs}ms で描画 `
      + `(実時間比 ${report.cost.realtimeFactor}x, 合成 ${report.cost.synthMs}ms, VGM ${report.cost.vgmBytes} bytes)`,
    );
  }
  if (!options.keep && fs.existsSync(workDir)) fs.rmSync(workDir, { recursive: true, force: true });
}

main();

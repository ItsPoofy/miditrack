// Inter-Onset Interval (IOI) autocorrelation and retro tempo detector.
// Analyzes note onset timestamps from sound chip commands to calculate the song's true musical BPM.

export interface DetectedTempoResult {
  bpm: number;
  isFrameSnapped: boolean;
}

export function detectTempoFromOnsets(
  onsetSampleTimes: number[],
  sampleRate = 44100
): number {
  if (!onsetSampleTimes || onsetSampleTimes.length < 6) {
    return 120;
  }

  // Sort and deduplicate attacks within 15ms (simultaneous chords / unison hits)
  const sorted = [...onsetSampleTimes].sort((a, b) => a - b);
  const minIntervalSamples = Math.round(sampleRate * 0.015);
  const attacks: number[] = [];
  for (const s of sorted) {
    if (attacks.length === 0 || s - attacks[attacks.length - 1] > minIntervalSamples) {
      attacks.push(s);
    }
  }

  if (attacks.length < 6) {
    return 120;
  }

  // Collect IOIs (Inter-Onset Intervals) within a 2.5 second window
  const maxIoiSamples = Math.round(sampleRate * 2.5);
  const minIoiSamples = Math.round(sampleRate * 0.06);
  const iois: number[] = [];

  for (let i = 0; i < attacks.length; i++) {
    for (let j = i + 1; j < attacks.length && attacks[j] - attacks[i] <= maxIoiSamples; j++) {
      const deltaSamples = attacks[j] - attacks[i];
      if (deltaSamples >= minIoiSamples) {
        iois.push(deltaSamples / sampleRate);
      }
    }
  }

  if (iois.length < 6) {
    return 120;
  }

  // Helper to score a candidate BPM
  function scoreBpm(candidateBpm: number): number {
    const beatSec = 60 / candidateBpm;
    const sub16 = beatSec / 4;

    // Tempo prior centered around 130 BPM with broad spread
    // Favors natural human musical tempo (80-160 BPM) and prevents sub-harmonic / 1.5x harmonic traps
    const logRatio = Math.log2(candidateBpm / 130);
    const prior = Math.exp(-0.5 * Math.pow(logRatio / 0.8, 2));

    let rawScore = 0;
    for (const ioi of iois) {
      const k = Math.round(ioi / sub16);
      if (k >= 1 && k <= 16) {
        const error = Math.abs(ioi - k * sub16);
        const relError = error / sub16;
        if (relError < 0.15) {
          const weight = (1 - relError / 0.15) * (1 / Math.sqrt(k));
          // Strong bonuses for fundamental metric pulses: 16th (1), 8th (2), quarter (4), half (8)
          const bonus = (k === 4 || k === 2) ? 1.8 : (k === 1 || k === 8 ? 1.3 : 1.0);
          rawScore += weight * bonus;
        }
      }
    }
    return rawScore * prior;
  }

  // Search candidate BPMs [60 .. 220] in 0.5 BPM steps
  let bestBpm = 120;
  let bestScore = -Infinity;

  for (let bpm = 60; bpm <= 220; bpm += 0.5) {
    const score = scoreBpm(bpm);
    if (score > bestScore) {
      bestScore = score;
      bestBpm = bpm;
    }
  }

  // Metric octave normalization:
  if (bestBpm < 90) {
    const doubled = bestBpm * 2;
    if (doubled <= 220 && scoreBpm(doubled) >= bestScore * 0.70) {
      bestBpm = doubled;
      bestScore = scoreBpm(doubled);
    }
  } else if (bestBpm > 185) {
    const halved = bestBpm / 2;
    if (halved >= 60 && scoreBpm(halved) >= bestScore * 0.80) {
      bestBpm = halved;
      bestScore = scoreBpm(halved);
    }
  }

  // Fine search around bestBpm in 0.002 BPM steps
  let fineBest = bestBpm;
  let fineScore = scoreBpm(bestBpm);
  for (let bpm = bestBpm - 1.5; bpm <= bestBpm + 1.5; bpm += 0.002) {
    const s = scoreBpm(bpm);
    if (s > fineScore) {
      fineScore = s;
      fineBest = bpm;
    }
  }

  // Check for exact retro sound-engine frame tempos:
  // 60Hz NTSC: 3600 / N (integer and half-frame steps: N=12, 12.5, ..., 60)
  // 50Hz PAL: 3000 / N
  let resolvedBpm = fineBest;
  let bestFrameCandidate: number | null = null;
  let minFrameDist = 0.5;

  for (let n = 24; n <= 120; n++) {
    const frames = n / 2;
    const c60 = 3600 / frames;
    const dist60 = Math.abs(c60 - fineBest);
    if (dist60 < minFrameDist && scoreBpm(c60) >= fineScore * 0.96) {
      minFrameDist = dist60;
      bestFrameCandidate = c60;
    }
    const c50 = 3000 / frames;
    const dist50 = Math.abs(c50 - fineBest);
    if (dist50 < minFrameDist && scoreBpm(c50) >= fineScore * 0.96) {
      minFrameDist = dist50;
      bestFrameCandidate = c50;
    }
  }

  if (bestFrameCandidate !== null) {
    resolvedBpm = bestFrameCandidate;
  }

  return Number(resolvedBpm.toFixed(4));
}

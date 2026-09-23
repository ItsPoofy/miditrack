export declare const MIDI_PPQ = 960;
export declare const DEFAULT_SAMPLE_RATE = 44100;
export declare function noiseDrumNote(normalizedRate: number, isPeriodic: boolean): number;
export declare function greatestCommonDivisor(left: number, right: number): number;
export declare function frequencyToMidiNote(frequency: number): number;
export declare function frequencyToExactMidi(frequency: number): number;
export declare function psgRegisterToFrequency(register: number, clockRate: number, flags: number): number;
export declare function ym2612FrequencyToHz(fnum: number, block: number, clockRate: number): number;
export declare function ym2203FrequencyToHz(fnum: number, block: number, clockRate: number, prescaler: number): number;
export declare function oplFrequencyToHz(fnum: number, block: number, clockRate: number): number;
export declare function ay8910RegisterToFrequency(register: number, clockRate: number, flags: number): number;
export declare function ym2203SSGRegisterToFrequency(register: number, clockRate: number, prescaler: number, flags: number): number;
export declare function huc6280RegisterToFrequency(register: number, clockRate: number): number;
export declare function ym2413RegisterToFrequency(fnum: number, block: number, clockRate: number): number;
export declare function gbDmgSquareFrequencyToHz(period: number, clockRate: number): number;
export declare function gbDmgWaveFrequencyToHz(period: number, clockRate: number): number;
export declare function gbDmgNoiseFrequencyToHz(nr43: number, clockRate: number): number;
export declare function gbDmgNoiseNoteForPeriod(nr43: number, clockRate: number): number;
export declare function samplesToTicks(samples: number, tempo: number, sampleRate?: number, ppq?: number): number;
/**
 * Snaps a MIDI tick timestamp to the nearest musical grid subdivision
 * if it falls within a jitter tolerance window (default: 35 ticks at 960 PPQ, ~15% of a 16th note).
 *
 * Checks standard musical subdivisions:
 * - Straight: multiples of 120 ticks (quarter=960, 8th=480, 16th=240, 32nd=120)
 * - Triplet: multiples of 80 ticks (triplet 8th=320, triplet 16th=160, triplet 32nd=80)
 *
 * Eliminates retro sound engine interrupt jitter and CPU register write latency,
 * ensuring note onsets and note offs land dead on DAW piano roll grid lines.
 */
export declare function snapTickToMusicalGrid(tick: number, tolerance?: number): number;

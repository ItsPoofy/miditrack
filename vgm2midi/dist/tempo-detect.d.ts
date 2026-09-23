export interface DetectedTempoResult {
    bpm: number;
    isFrameSnapped: boolean;
}
export declare function detectTempoFromOnsets(onsetSampleTimes: number[], sampleRate?: number): number;

/** トラック編集の未送信変更、デバウンス、直列化を所有するコントローラー。 */

/**
 * トラック設定の複数変更を1回の処理へまとめる。
 *
 * @param {{ commit: (edits: { assignments: object, volumes: object, sources: object }) => Promise<boolean>, debounceMs: number, setTimer?: typeof setTimeout, clearTimer?: typeof clearTimeout }} options
 */
export function createTrackEditController({
  commit,
  debounceMs,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  let pendingAssignments = {};
  let pendingVolumes = {};
  let pendingSources = {};
  let pendingChannels = {};
  let pendingNames = {};
  let flushTimer = null;
  let activeFlush = null;

  function queueEdits({ assignments = {}, volumes = {}, sources = {}, channels = {}, names = {} }) {
    Object.assign(pendingAssignments, assignments);
    Object.assign(pendingVolumes, volumes);
    Object.assign(pendingSources, sources);
    Object.assign(pendingChannels, channels);
    Object.assign(pendingNames, names);
  }

  function queueAssignment(trackIndex, program) {
    queueEdits({ assignments: { [trackIndex]: program } });
  }

  function queueVolume(trackIndex, volumePercent) {
    queueEdits({ volumes: { [trackIndex]: volumePercent } });
  }

  function queueSource(trackIndex, source) {
    queueEdits({ sources: { [trackIndex]: source } });
  }

  function queueChannel(trackIndex, channel) {
    queueEdits({ channels: { [trackIndex]: channel } });
  }

  function queueName(trackIndex, name) {
    queueEdits({ names: { [trackIndex]: name } });
  }

  function hasPending() {
    return Object.keys(pendingAssignments).length > 0
      || Object.keys(pendingVolumes).length > 0
      || Object.keys(pendingSources).length > 0
      || Object.keys(pendingChannels).length > 0
      || Object.keys(pendingNames).length > 0;
  }

  function hasPendingVolume(trackIndex) {
    return trackIndex in pendingVolumes;
  }

  function cancelScheduledFlush() {
    if (flushTimer !== null) clearTimer(flushTimer);
    flushTimer = null;
  }

  function scheduleFlush() {
    cancelScheduledFlush();
    flushTimer = setTimer(() => {
      flushTimer = null;
      void flush();
    }, debounceMs);
  }

  function takePendingEdits() {
    const edits = {
      assignments: pendingAssignments,
      volumes: pendingVolumes,
      sources: pendingSources,
      channels: pendingChannels,
      names: pendingNames,
    };
    pendingAssignments = {};
    pendingVolumes = {};
    pendingSources = {};
    pendingChannels = {};
    pendingNames = {};
    return edits;
  }

  async function flush() {
    cancelScheduledFlush();
    if (activeFlush) {
      const didSucceed = await activeFlush;
      return didSucceed ? flush() : false;
    }
    if (!hasPending()) return true;
    const currentFlush = Promise.resolve(commit(takePendingEdits())).catch(() => false);
    activeFlush = currentFlush;
    const didSucceed = await currentFlush;
    if (activeFlush === currentFlush) activeFlush = null;
    return didSucceed ? flush() : false;
  }

  return {
    cancelScheduledFlush,
    flush,
    hasPending,
    hasPendingVolume,
    queueAssignment,
    queueChannel,
    queueEdits,
    queueName,
    queueSource,
    queueVolume,
    scheduleFlush,
  };
}

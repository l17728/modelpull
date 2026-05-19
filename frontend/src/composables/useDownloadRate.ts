import { onUnmounted, ref, watch, type Ref } from 'vue'

export interface RateSample { t: number; bytes: number }
export interface RateResult {
  currentBps: number
  avgBps: number
  etaSeconds: number | null
}

const MAX_SAMPLES = 30

/** Pure: derive current (last-window) + average B/s and ETA from samples. */
export function computeRate(
  samples: RateSample[], bytesTotal: number | null,
): RateResult {
  if (samples.length < 2) {
    return { currentBps: 0, avgBps: 0, etaSeconds: null }
  }
  const first = samples[0]
  const last = samples[samples.length - 1]
  if (!first || !last) {
    return { currentBps: 0, avgBps: 0, etaSeconds: null }
  }
  const spanSec = (last.t - first.t) / 1000
  const avgBps = spanSec > 0
    ? Math.max(0, (last.bytes - first.bytes) / spanSec) : 0

  const tail = samples.slice(-5)
  const tf = tail[0]
  const tl = tail[tail.length - 1]
  let currentBps = 0
  if (tf && tl && tl.t > tf.t) {
    currentBps = Math.max(0, (tl.bytes - tf.bytes) / ((tl.t - tf.t) / 1000))
  }

  let etaSeconds: number | null = null
  if (bytesTotal !== null && currentBps > 0) {
    const remaining = Math.max(0, bytesTotal - last.bytes)
    etaSeconds = remaining / currentBps
  }
  return { currentBps, avgBps, etaSeconds }
}

/** Composable: sample a reactive byte counter over time → reactive RateResult. */
export function useDownloadRate(
  bytesDone: Ref<number | null | undefined>,
  bytesTotal: Ref<number | null | undefined>,
) {
  const samples = ref<RateSample[]>([])
  const result = ref<RateResult>({
    currentBps: 0, avgBps: 0, etaSeconds: null,
  })

  function recompute() {
    result.value = computeRate(samples.value, bytesTotal.value ?? null)
  }

  const stop = watch(bytesDone, (v) => {
    if (v === null || v === undefined) return
    samples.value.push({ t: Date.now(), bytes: v })
    if (samples.value.length > MAX_SAMPLES) samples.value.shift()
    recompute()
  }, { immediate: true })

  onUnmounted(stop)
  return { rate: result }
}

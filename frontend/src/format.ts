/**
 * Presentation helpers that turn contract values (seconds, hertz, bytes,
 * ffprobe format names, telemetry fractions) into the short human-readable
 * strings the UI shows.
 *
 * These are pure functions with no React or DOM dependency so they can be
 * reused by any component and unit-tested directly.
 *
 * **Size-unit convention:** file sizes use *binary* units (1024-based) with
 * the conventional `B`/`KB`/`MB`/`GB`/`TB` labels — the same convention
 * Windows Explorer uses. A 44,771,328-byte FLAC therefore reads `42.7 MB`,
 * not `44.8 MB`. This is applied consistently across the app.
 */

/** Binary size unit labels, ascending; each step is a factor of 1024. */
const SIZE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const

/** Bytes per step between adjacent {@link SIZE_UNITS} entries. */
const BYTES_PER_UNIT = 1024

/**
 * Container formats whose audio is PCM by definition; naming the `pcm_*`
 * codec alongside them adds nothing the bit-depth row does not already say.
 */
const PCM_CONTAINERS = new Set(['wav', 'wave', 'w64', 'aiff', 'aif', 'aifc'])

/** Drop a trailing `.0` from a fixed-decimal string (`"48.0"` → `"48"`). */
function trimTrailingZero(value: string): string {
  return value.endsWith('.0') ? value.slice(0, -2) : value
}

/**
 * Format a duration in seconds as `m:ss`, or `h:mm:ss` once it reaches an
 * hour. Sub-second values round to the nearest second; negative and
 * non-finite inputs render as `0:00`.
 *
 * @example formatDuration(227.4) // "3:47"
 * @example formatDuration(3725) // "1:02:05"
 */
export function formatDuration(seconds: number): string {
  const total =
    Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds) : 0
  const secondsPart = String(total % 60).padStart(2, '0')
  const minutes = Math.floor(total / 60) % 60
  const hours = Math.floor(total / 3600)
  if (hours > 0) {
    return `${String(hours)}:${String(minutes).padStart(2, '0')}:${secondsPart}`
  }
  return `${String(minutes)}:${secondsPart}`
}

/**
 * Format the container/codec pair for display. The container is shown
 * uppercased; the codec is appended in parentheses only when it differs
 * meaningfully from the container — identical names (`flac`/`flac`) and PCM
 * in a PCM-only container (`wav`/`pcm_s24le`) show the container alone.
 *
 * @example formatAudioFormat('flac', 'flac') // "FLAC"
 * @example formatAudioFormat('wav', 'pcm_s24le') // "WAV"
 * @example formatAudioFormat('mov', 'aac') // "MOV (AAC)"
 */
export function formatAudioFormat(container: string, codec: string): string {
  const containerName = container.trim()
  const codecName = codec.trim()
  const containerKey = containerName.toLowerCase()
  const codecKey = codecName.toLowerCase()

  if (codecName === '' || codecKey === containerKey) {
    return containerName.toUpperCase()
  }
  if (containerName === '') {
    return codecName.toUpperCase()
  }
  if (PCM_CONTAINERS.has(containerKey) && codecKey.startsWith('pcm')) {
    return containerName.toUpperCase()
  }
  return `${containerName.toUpperCase()} (${codecName.toUpperCase()})`
}

/**
 * Format a channel count: `Mono` for 1, `Stereo` for 2, and `N channels`
 * for anything else (a 5.1 mix reads `6 channels`).
 */
export function formatChannels(channels: number): string {
  if (channels === 1) {
    return 'Mono'
  }
  if (channels === 2) {
    return 'Stereo'
  }
  return `${String(channels)} channels`
}

/**
 * Format a sample rate in hertz as kilohertz with at most one decimal
 * place (`44100` → `44.1 kHz`, `48000` → `48 kHz`).
 */
export function formatSampleRate(sampleRateHz: number): string {
  return `${trimTrailingZero((sampleRateHz / 1000).toFixed(1))} kHz`
}

/**
 * Format bits per sample as `N bit`, or `null` when the backend reported no
 * bit depth (lossy formats) so the caller can omit the row entirely.
 */
export function formatBitDepth(bitDepth: number | null): string | null {
  return bitDepth === null ? null : `${String(bitDepth)} bit`
}

/**
 * Format a bit rate as `N kbps` (rounded to the nearest kilobit), or `null`
 * when the backend reported none so the caller can omit the row entirely.
 */
export function formatBitRate(bitRateBps: number | null): string | null {
  return bitRateBps === null
    ? null
    : `${String(Math.round(bitRateBps / 1000))} kbps`
}

/**
 * Format a byte count using binary units with conventional labels
 * (`44771328` → `42.7 MB`). Whole bytes are shown without a decimal;
 * larger units keep one decimal place with a trailing `.0` trimmed.
 * Negative and non-finite inputs render as `0 B`.
 */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return '0 B'
  }
  let value = bytes
  let unitIndex = 0
  while (value >= BYTES_PER_UNIT && unitIndex < SIZE_UNITS.length - 1) {
    value /= BYTES_PER_UNIT
    unitIndex += 1
  }
  if (unitIndex === 0) {
    return `${String(Math.round(value))} B`
  }
  let rendered = trimTrailingZero(value.toFixed(1))
  // Rounding can push the value up into the next unit (1023.97 KB → 1024 KB).
  if (Number(rendered) >= BYTES_PER_UNIT && unitIndex < SIZE_UNITS.length - 1) {
    unitIndex += 1
    rendered = trimTrailingZero((value / BYTES_PER_UNIT).toFixed(1))
  }
  return `${rendered} ${SIZE_UNITS[unitIndex] ?? 'B'}`
}

/**
 * Format a memory figure given in **mebibytes** — the unit every
 * `ModelRequirements` field uses (`recommended_vram_mb`, `minimum_vram_mb`,
 * `minimum_ram_mb`) — with the same binary units and labels as
 * {@link formatFileSize}, so "6 GB of VRAM" and "870 MB to download" are
 * measured the same way on the same screen.
 *
 * @example formatMemorySize(6144) // "6 GB"
 * @example formatMemorySize(512) // "512 MB"
 */
export function formatMemorySize(mebibytes: number): string {
  return formatFileSize(mebibytes * BYTES_PER_UNIT * BYTES_PER_UNIT)
}

/**
 * Format a `0..1` fraction as a whole-number percentage (`0.91` → `91%`),
 * used for GPU utilization. The contract documents the range, so values
 * above `1` are clamped to `100%` rather than rendered as `140%`; negative
 * and non-finite inputs render as `0%`.
 *
 * @example formatPercentage(0.91) // "91%"
 * @example formatPercentage(0.004) // "0%"
 * @example formatPercentage(1) // "100%"
 */
export function formatPercentage(fraction: number): string {
  if (!Number.isFinite(fraction) || fraction <= 0) {
    return '0%'
  }
  return `${String(Math.round(Math.min(fraction, 1) * 100))}%`
}

/**
 * Format a temperature in degrees Celsius, rounded to the nearest degree
 * (`71.4` → `71 °C`). Sub-zero readings are kept — a device colder than
 * freezing is unusual, not impossible — but non-finite input renders as
 * `0 °C`.
 *
 * @example formatTemperature(63) // "63 °C"
 * @example formatTemperature(71.4) // "71 °C"
 */
export function formatTemperature(celsius: number): string {
  const value = Number.isFinite(celsius) ? Math.round(celsius) : 0
  // `Math.round(-0.2)` is `-0`; adding zero normalises it so it reads "0 °C".
  return `${String(value + 0)} °C`
}

/**
 * Format a real-time factor — this project's standard performance metric,
 * `audio duration / processing duration` (ARCHITECTURE.md §12) — as a
 * multiplier. One decimal place from `1×` upwards, two below it so a run
 * slower than real time does not collapse to `0×`; trailing zeros are
 * dropped. Non-positive, non-finite, and vanishingly small values render as
 * `0×`.
 *
 * @example formatRealtimeFactor(7.9) // "7.9×"
 * @example formatRealtimeFactor(12) // "12×"
 * @example formatRealtimeFactor(0.42) // "0.42×"
 */
export function formatRealtimeFactor(factor: number): string {
  if (!Number.isFinite(factor) || factor <= 0) {
    return '0×'
  }
  return `${String(Number(factor.toFixed(factor < 1 ? 2 : 1)))}×`
}

import { describe, expect, it } from 'vitest'
import {
  formatAudioFormat,
  formatBitDepth,
  formatBitRate,
  formatChannels,
  formatDuration,
  formatFileSize,
  formatPercentage,
  formatRealtimeFactor,
  formatSampleRate,
  formatTemperature,
} from './format'

describe('formatDuration', () => {
  it.each([
    [0, '0:00'],
    [1, '0:01'],
    [9, '0:09'],
    [59, '0:59'],
    [60, '1:00'],
    [227.4, '3:47'],
    [227.6, '3:48'],
    [599, '9:59'],
    [3599, '59:59'],
    [3600, '1:00:00'],
    [3725, '1:02:05'],
    [7384.2, '2:03:04'],
    [-5, '0:00'],
    [Number.NaN, '0:00'],
  ])('formats %p as %p', (seconds, expected) => {
    expect(formatDuration(seconds)).toBe(expected)
  })
})

describe('formatAudioFormat', () => {
  it.each([
    ['flac', 'flac', 'FLAC'],
    ['mp3', 'mp3', 'MP3'],
    ['wav', 'pcm_s16le', 'WAV'],
    ['wav', 'pcm_s24le', 'WAV'],
    ['aiff', 'pcm_s16be', 'AIFF'],
    ['mov', 'aac', 'MOV (AAC)'],
    ['ogg', 'vorbis', 'OGG (VORBIS)'],
    ['ogg', 'opus', 'OGG (OPUS)'],
    ['FLAC', 'FLAC', 'FLAC'],
    ['flac', '', 'FLAC'],
    ['', 'aac', 'AAC'],
    ['', '', ''],
  ])('formats container %p / codec %p as %p', (container, codec, expected) => {
    expect(formatAudioFormat(container, codec)).toBe(expected)
  })
})

describe('formatChannels', () => {
  it.each([
    [1, 'Mono'],
    [2, 'Stereo'],
    [6, '6 channels'],
    [8, '8 channels'],
    [0, '0 channels'],
  ])('formats %p as %p', (channels, expected) => {
    expect(formatChannels(channels)).toBe(expected)
  })
})

describe('formatSampleRate', () => {
  it.each([
    [8000, '8 kHz'],
    [22050, '22.1 kHz'],
    [44100, '44.1 kHz'],
    [48000, '48 kHz'],
    [88200, '88.2 kHz'],
    [96000, '96 kHz'],
    [192000, '192 kHz'],
  ])('formats %p Hz as %p', (hz, expected) => {
    expect(formatSampleRate(hz)).toBe(expected)
  })
})

describe('formatBitDepth', () => {
  it.each([
    [16, '16 bit'],
    [24, '24 bit'],
    [32, '32 bit'],
    [null, null],
  ])('formats %p as %p', (bitDepth, expected) => {
    expect(formatBitDepth(bitDepth)).toBe(expected)
  })
})

describe('formatBitRate', () => {
  it.each([
    [320000, '320 kbps'],
    [128000, '128 kbps'],
    [1411000, '1411 kbps'],
    [96500, '97 kbps'],
    [null, null],
  ])('formats %p as %p', (bitRate, expected) => {
    expect(formatBitRate(bitRate)).toBe(expected)
  })
})

describe('formatFileSize', () => {
  it.each([
    [0, '0 B'],
    [-1, '0 B'],
    [1, '1 B'],
    [999, '999 B'],
    [1023, '1023 B'],
    [1024, '1 KB'],
    [1536, '1.5 KB'],
    [1048575, '1 MB'],
    [1048576, '1 MB'],
    [44771328, '42.7 MB'],
    [1073741824, '1 GB'],
    [3221225472, '3 GB'],
    [1099511627776, '1 TB'],
    [5629499534213120, '5120 TB'],
  ])('formats %p bytes as %p', (bytes, expected) => {
    expect(formatFileSize(bytes)).toBe(expected)
  })
})

describe('formatPercentage', () => {
  it.each([
    [0, '0%'],
    [0.004, '0%'],
    [0.005, '1%'],
    [0.42, '42%'],
    [0.91, '91%'],
    [0.995, '100%'],
    [1, '100%'],
    // Out of the documented [0, 1] range: clamped, never "140%".
    [1.4, '100%'],
    [-0.2, '0%'],
    [Number.NaN, '0%'],
    [Number.POSITIVE_INFINITY, '0%'],
    [Number.NEGATIVE_INFINITY, '0%'],
  ])('formats %p as %p', (fraction, expected) => {
    expect(formatPercentage(fraction)).toBe(expected)
  })
})

describe('formatTemperature', () => {
  it.each([
    [63, '63 °C'],
    [71.4, '71 °C'],
    [71.5, '72 °C'],
    [0, '0 °C'],
    // Negative readings are kept; -0 must not render as "-0 °C".
    [-3, '-3 °C'],
    [-0.2, '0 °C'],
    [Number.NaN, '0 °C'],
    [Number.POSITIVE_INFINITY, '0 °C'],
  ])('formats %p as %p', (celsius, expected) => {
    expect(formatTemperature(celsius)).toBe(expected)
  })
})

describe('formatRealtimeFactor', () => {
  it.each([
    [7.9, '7.9×'],
    [7.84, '7.8×'],
    [1, '1×'],
    [12, '12×'],
    [12.04, '12×'],
    // Below 1x (slower than real time) keeps two decimals.
    [0.999, '1×'],
    [0.42, '0.42×'],
    [0.4, '0.4×'],
    [0.005, '0.01×'],
    [0.001, '0×'],
    [0, '0×'],
    [-5, '0×'],
    [Number.NaN, '0×'],
    [Number.POSITIVE_INFINITY, '0×'],
  ])('formats %p as %p', (factor, expected) => {
    expect(formatRealtimeFactor(factor)).toBe(expected)
  })
})

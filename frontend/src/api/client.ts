/**
 * Minimal typed HTTP client for the Straticate backend.
 *
 * All commands go over REST under the `/api/v1` prefix (proxied to the
 * FastAPI backend in development); progress/telemetry will use
 * WebSockets in later features.
 */

import type { HealthStatus, VersionInfo } from './types'

/** Payload of the backend error envelope: `{"error": {code, message, detail}}`. */
export interface ApiErrorBody {
  code: string
  message: string
  detail?: unknown
}

/** Error thrown when the backend responds with a non-2xx status. */
export class ApiError extends Error {
  /** HTTP status code of the failed response. */
  readonly status: number
  /** Machine-readable error code from the backend envelope. */
  readonly code: string
  /** Optional structured detail from the backend envelope. */
  readonly detail: unknown

  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.detail = body.detail
  }
}

const API_BASE = '/api/v1'

async function parseErrorBody(response: Response): Promise<ApiErrorBody> {
  try {
    const payload: unknown = await response.json()
    if (
      typeof payload === 'object' &&
      payload !== null &&
      'error' in payload &&
      typeof payload.error === 'object' &&
      payload.error !== null
    ) {
      const error = payload.error as Partial<ApiErrorBody>
      return {
        code: typeof error.code === 'string' ? error.code : 'unknown_error',
        message:
          typeof error.message === 'string'
            ? error.message
            : `HTTP ${String(response.status)}`,
        detail: error.detail,
      }
    }
  } catch {
    // Body was not JSON; fall through to the generic error below.
  }
  return {
    code: 'unknown_error',
    message: `HTTP ${String(response.status)}`,
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorBody(response))
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

/** Perform a GET request against the backend and parse the JSON response. */
export function get<T>(path: string): Promise<T> {
  return request<T>(path)
}

/** Perform a POST request with an optional JSON body and parse the JSON response. */
export function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

/** Fetch the backend health status. */
export function getHealth(): Promise<HealthStatus> {
  return get<HealthStatus>('/health')
}

/** Fetch the backend version. */
export function getVersion(): Promise<VersionInfo> {
  return get<VersionInfo>('/version')
}

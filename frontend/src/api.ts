import type { ApiResult, State } from './types';

let tokenValue: string | undefined;
let tokenPromise: Promise<string> | undefined;
const requests = new Set<AbortController>();
const REQUEST_TIMEOUT = 20_000;

export function isCancelled(problem: unknown): boolean {
  return problem instanceof Error && problem.name === 'AbortError';
}

export function cancelPendingRequests(): void {
  for (const controller of requests) controller.abort();
  requests.clear();
  if (!tokenValue) tokenPromise = undefined;
}

function waitFor<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener('abort', abort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
  });
}

export async function request<T>(path: string, init: RequestInit = {}, signal?: AbortSignal, authenticated = false): Promise<T> {
  signal?.throwIfAborted();
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason);
  signal?.addEventListener('abort', abort, { once: true });
  requests.add(controller);
  const timeout = setTimeout(() => controller.abort(new DOMException('The local service took too long to answer.', 'TimeoutError')), REQUEST_TIMEOUT);
  try {
    const token = authenticated ? await sessionToken(controller.signal) : undefined;
    controller.signal.throwIfAborted();
    const response = await fetch('/api/' + path, {
      credentials: 'same-origin', ...init, signal: controller.signal,
      headers: { ...init.headers, ...(token ? { 'X-Showdown-Token': token } : {}) },
    });
    let data: ApiResult;
    try { data = await response.json(); }
    catch (problem) {
      if (controller.signal.aborted) throw controller.signal.reason;
      throw new Error('The local service did not answer correctly.', { cause: problem });
    }
    controller.signal.throwIfAborted();
    if (!response.ok || data.ok === false) throw new Error(data.error || 'The action could not be completed.');
    return data as T;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener('abort', abort);
    requests.delete(controller);
  }
}

export async function sessionToken(signal?: AbortSignal): Promise<string> {
  signal?.throwIfAborted();
  if (tokenValue) return tokenValue;
  if (!tokenPromise) {
    const pending = request<ApiResult>('session').then(data => {
      if (!data.token) throw new Error('Local session unavailable. Reopen the app.');
      tokenValue = data.token;
      return data.token;
    });
    tokenPromise = pending;
    void pending.catch(() => { if (tokenPromise === pending) tokenPromise = undefined; });
  }
  return waitFor(tokenPromise, signal);
}

export async function readState(signal?: AbortSignal): Promise<State> {
  const data = await request<State & { state?: State }>('state', {}, signal, true);
  return data.state ?? data;
}

export async function post<T extends ApiResult = ApiResult>(path: string, data: unknown = {}, options: { signal?: AbortSignal } = {}): Promise<T> {
  return request<T>(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
  }, options.signal, true);
}

export async function readReport(battleId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const data = await request<ApiResult & Record<string, unknown>>('report?battle_id=' + encodeURIComponent(battleId), {}, signal, true);
  return data.report ?? data;
}

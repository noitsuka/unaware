import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('local API contract', () => {
  beforeEach(() => vi.resetModules());
  afterEach(() => vi.unstubAllGlobals());

  it('bootstraps one session and sends the CSRF token on every mutation', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ token: 'local-token' })))
      .mockImplementation(async () => new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal('fetch', fetcher);
    const api = await import('./api');
    await api.post('recover');
    await api.post('stop');
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(fetcher.mock.calls[1][0]).toBe('/api/recover');
    expect(fetcher.mock.calls[1][1].headers['X-Showdown-Token']).toBe('local-token');
    expect(fetcher.mock.calls[2][1].credentials).toBe('same-origin');
  });
  it('reports the actual server validation error instead of treating failed actions as success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ token: 'local-token' })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: false, error: 'Invalid EVs.' }), { status: 400 })));
    const api = await import('./api');
    await expect(api.post('team', { name: 'Example', text: 'invalid' })).rejects.toThrow('Invalid EVs.');
  });
  it('keeps the complete private password only in the requested start payload', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ token: 'local-token' })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal('fetch', fetcher);
    const api = await import('./api');
    await api.post('start', { username: 'TestBot', password: 'private', team: 'Pikachu' });
    expect(JSON.parse(fetcher.mock.calls[1][1].body).password).toBe('private');
    expect(fetcher.mock.calls[0][0]).toBe('/api/session');
    expect(fetcher.mock.calls[0][1]).not.toHaveProperty('body');
  });
  it('sends a boolean autoplay update independently of the current match', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ token: 'local-token' })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, state: { running: true } })));
    vi.stubGlobal('fetch', fetcher);
    const api = await import('./api');
    const result = await api.post('autoplay', { enabled: false });
    expect(fetcher.mock.calls[1][0]).toBe('/api/autoplay');
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ enabled: false });
    expect(result.state?.running).toBe(true);
  });
});

describe('request cancellation', () => {
  beforeEach(() => vi.resetModules());
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

  const hanging = (_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
  });

  it('STOP cancels a pending start and sends immediately with the cached session', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ token: 'local-token' })))
      .mockImplementationOnce(hanging).mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal('fetch', fetcher);
    const api = await import('./api');
    const pending = api.post('start');
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    api.cancelPendingRequests();
    await api.post('stop');
    await rejected;
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/session', '/api/start', '/api/stop']);
    expect(fetcher.mock.calls[1][1].signal.aborted).toBe(true);
  });

  it('a cancelled bootstrap does not block an emergency stop behind the old promise', async () => {
    const fetcher = vi.fn().mockImplementationOnce(hanging)
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: 'new-token' })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal('fetch', fetcher);
    const api = await import('./api');
    const pending = api.readState();
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    api.cancelPendingRequests();
    await api.post('stop');
    await rejected;
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/session', '/api/session', '/api/stop']);
  });

  it('closing a report cancels the HTTP request and clears its timeout', async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ token: 'local-token' })))
      .mockImplementationOnce(hanging);
    vi.stubGlobal('fetch', fetcher);
    const api = await import('./api');
    await api.sessionToken();
    const controller = new AbortController();
    const pending = api.readReport('example', controller.signal);
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    await Promise.resolve(); await Promise.resolve();
    controller.abort();
    await rejected;
    expect(vi.getTimerCount()).toBe(0);
  });
});

// 서버 통신. 오래 걸리는 작업은 job id 를 받아 진행률을 폴링한다.

async function request(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(url, opt);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  get:    (u)    => request('GET', u),
  post:   (u, b) => request('POST', u, b ?? {}),
  patch:  (u, b) => request('PATCH', u, b ?? {}),
  del:    (u)    => request('DELETE', u),

  async upload(files) {
    const fd = new FormData();
    for (const f of files) fd.append('files', f, f.name);
    const res = await fetch('/api/images/upload', { method: 'POST', body: fd });
    if (!res.ok) throw new Error('업로드 실패');
    return res.json();
  },
};

// job 을 끝까지 따라가며 onTick(stage, frac, message) 를 호출한다.
export async function follow(jobId, onTick) {
  for (;;) {
    const j = await api.get(`/api/job/${jobId}`);
    onTick?.(j);
    if (j.done) {
      if (j.error) throw new Error(j.error);
      return j.result;
    }
    await new Promise(r => setTimeout(r, 180));
  }
}

// Meridian — 메인 앱. 상태를 한 곳에 두고 패널·뷰를 거기에 맞춰 다시 그린다.

import { api, follow } from './api.js';
import { PanoView, CPPane } from './viewers.js';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const S = {
  project: null,
  selected: null,        // 필름스트립에서 고른 이미지 id
  view: 'preview',
  preview: null,         // { url, width, height, layout, full_size, megapixels }
  cpPair: [null, null],
  cpPoints: [],
  pendingA: null,        // 제어점 찍는 중: 왼쪽에 먼저 찍은 점
  busy: false,
};

/* ------------------------------------------------------------ 알림·진행률 */

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind ? 'toast--' + kind : ''}`;
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 3400);
  setTimeout(() => el.remove(), 3800);
}

function progressOn(title) {
  $('#prog-title').textContent = title;
  $('#prog-msg').textContent = '…';
  $('#prog-fill').style.width = '0%';
  $('#prog-pct').textContent = '0%';
  $('#scrim').hidden = false;
  S.busy = true;
}
function progressTick(j) {
  $('#prog-msg').textContent = j.message || '';
  const pct = Math.round((j.frac || 0) * 100);
  $('#prog-fill').style.width = `${pct}%`;
  $('#prog-pct').textContent = `${pct}%`;
}
function progressOff() { $('#scrim').hidden = true; S.busy = false; }

async function job(title, starter) {
  progressOn(title);
  try {
    const r = await starter();
    if (r?.cancelled) return null;
    if (!r?.job) return r;
    return await follow(r.job, progressTick);
  } finally {
    progressOff();
  }
}

/* ------------------------------------------------------------ 상태 반영 */

async function reload() {
  S.project = await api.get('/api/project');
  paintAll();
}

function paintAll() {
  paintStrip();
  paintStatus();
  paintPanel();
  paintCPSelectors();
  if (S.view === 'cplist') paintCPTable();
}

function paintStatus() {
  const p = S.project;
  const n = Object.keys(p.images).length;
  const st = p.cp_stats;
  $('#st-images').innerHTML = `사진 <b>${n}</b>`;
  $('#st-cp').innerHTML = `제어점 <b>${st.enabled}</b>${st.total !== st.enabled ? `/${st.total}` : ''}`;

  const rmsEl = $('#st-rms');
  if (p.optimized) {
    const r = p.last_rms;
    rmsEl.innerHTML = `RMS <b>${r.toFixed(2)}px</b>`;
    rmsEl.className = 'status__item ' + (r < 5 ? 'status__item--good' : r < 12 ? 'status__item--warn' : 'status__item--bad');
  } else {
    rmsEl.innerHTML = '정렬 전';
    rmsEl.className = 'status__item';
  }

  const sizeEl = $('#st-size');
  const groups = st.groups || [];
  if (n && groups.length > 1) {
    sizeEl.innerHTML = `<b>${groups.length}조각</b>으로 끊김`;
    sizeEl.className = 'status__item status__item--bad';
  } else if (S.preview) {
    sizeEl.innerHTML = `출력 <b>${S.preview.full_size[0]}×${S.preview.full_size[1]}</b>`;
    sizeEl.className = 'status__item';
  } else {
    sizeEl.innerHTML = '—';
    sizeEl.className = 'status__item';
  }
}

function paintStrip() {
  const p = S.project;
  const ids = Object.keys(p.images).map(Number).sort((a, b) => a - b);
  $('#rail-count').textContent = ids.length;
  $('#rail-hint').hidden = ids.length > 0;

  const strip = $('#strip');
  strip.innerHTML = '';
  for (const id of ids) {
    const im = p.images[id];
    const el = document.createElement('div');
    el.className = 'thumb' + (S.selected === id ? ' thumb--sel' : '') + (im.enabled ? '' : ' thumb--off');
    el.innerHTML = `
      <img src="/api/thumb/${id}" alt="" loading="lazy">
      <div class="thumb__tag">${id + 1}</div>
      <div class="thumb__badges">
        ${p.anchor === id ? '<span class="badge badge--anchor">기준</span>' : ''}
        ${im.is_raw ? '<span class="badge badge--raw">RAW</span>' : ''}
      </div>
      <div class="thumb__name" title="${im.name}">${im.name}</div>`;
    el.onclick = () => { S.selected = id; paintStrip(); paintPanel(); };
    el.oncontextmenu = e => { e.preventDefault(); imageMenu(id); };
    strip.append(el);
  }
}

async function imageMenu(id) {
  const im = S.project.images[id];
  const acts = [
    `${im.enabled ? '이 사진 빼기' : '이 사진 넣기'}`,
    S.project.anchor === id ? null : '기준 사진으로',
    '프로젝트에서 삭제',
  ].filter(Boolean);
  const pick = prompt(`${im.name}\n\n` + acts.map((a, i) => `${i + 1}. ${a}`).join('\n') + '\n\n번호 입력:');
  const k = parseInt(pick, 10) - 1;
  if (isNaN(k) || !acts[k]) return;
  const a = acts[k];
  if (a.includes('빼기') || a.includes('넣기')) {
    S.project = await api.patch(`/api/images/${id}`, { enabled: !im.enabled });
  } else if (a.includes('기준')) {
    S.project = await api.patch(`/api/images/${id}`, { anchor: true });
  } else {
    S.project = await api.del(`/api/images/${id}`);
    if (S.selected === id) S.selected = null;
  }
  paintAll();
}

/* ------------------------------------------------------------ 속성 패널 */

function paintPanel() {
  const p = S.project;
  const s = p.settings;

  for (const b of $$('#seg-proj .seg__b, #seg-proj2 .seg__b'))
    b.classList.toggle('seg__b--on', b.dataset.v === s.projection);
  $('#sel-seam').value = s.seam;
  $('#sel-blend').value = s.blender;
  $('#sel-exposure').value = s.exposure;
  $('#chk-wb').checked = s.per_channel;
  $('#chk-vig').checked = s.vignetting;
  $('#rng-lf').value = Math.round((s.low_freq ?? 1) * 100);
  $('#lbl-lf').textContent = `${Math.round((s.low_freq ?? 1) * 100)}%`;
  const vg = p.vignetting || {};
  const keys = Object.keys(vg);
  $('#vig-note').textContent = keys.length
    ? keys.map(k => {
        const v = vg[k], f = 1 + v.a + v.b + v.c;     // 반경 1.0 에서의 밝기
        return `렌즈 ${Number(k) + 1}: 가장자리 ${(f * 100).toFixed(1)}%`;
      }).join('  ')
    : '정렬하면 비네팅을 잽니다';
  $('#sel-format').value = s.format;
  $('#rng-quality').value = s.quality;
  $('#lbl-quality').textContent = s.quality;
  $('#fld-quality').hidden = s.format !== 'jpg';
  $('#rng-scale').value = s.scale_percent;
  $('#lbl-scale').textContent = `${Math.round(s.scale_percent)}%`;

  if (p.layout) {
    $('#rng-yaw').value = (p.layout.center_yaw * 180 / Math.PI).toFixed(1);
    $('#lbl-yaw').textContent = `${(p.layout.center_yaw * 180 / Math.PI).toFixed(0)}°`;
    $('#rng-pitch').value = (p.layout.center_pitch * 180 / Math.PI).toFixed(1);
    $('#lbl-pitch').textContent = `${(p.layout.center_pitch * 180 / Math.PI).toFixed(0)}°`;
  }

  // 렌즈 카드
  const ll = $('#lens-list');
  ll.innerHTML = '';
  const lensIds = Object.keys(p.lenses);
  if (!lensIds.length) ll.innerHTML = '<div class="meta">사진을 추가하면 렌즈가 잡힙니다</div>';
  for (const lid of lensIds) {
    const l = p.lenses[lid];
    const userIds = p.lens_users?.[lid] || [];
    const users = userIds.length;
    // 렌즈 이름과 초점거리는 이 렌즈를 쓰는 첫 사진의 EXIF 에서 가져온다
    const ex = userIds.length ? (p.images[userIds[0]]?.exif || {}) : {};
    const lensName = ex.lens || [ex.make, ex.model].filter(Boolean).join(' ') || '이름 없는 렌즈';
    const focal = ex.focal_length ? `${ex.focal_length}mm` : '';
    const focal35 = ex.focal_35 && ex.focal_35 !== ex.focal_length ? ` (35mm 환산 ${ex.focal_35}mm)` : '';
    const hint = userIds.length ? p.images[userIds[0]] : null;
    const card = document.createElement('div');
    card.className = 'lens-card';
    card.innerHTML = `
      <div class="lens-card__head"><span>렌즈 ${Number(lid) + 1}</span><span>사진 ${users}장</span></div>
      <div class="lens-card__name" title="${lensName}">${lensName}</div>
      ${focal ? `<div class="meta__row"><span>초점거리</span><span>${focal}${focal35}</span></div>` : ''}
      ${hint ? `<div class="meta__row"><span>EXIF 화각</span><span>${hint.fov_hint.toFixed(1)}°</span></div>` : ''}
      <div class="field">
        <label>화각<span>${l.fov.toFixed(2)}°</span></label>
        <input type="range" min="8" max="220" step="0.25" value="${l.fov}" data-lens="${lid}" data-k="fov">
      </div>
      <div class="meta__row"><span>왜곡 a / b / c</span>
        <span>${l.a.toFixed(4)} / ${l.b.toFixed(4)} / ${l.c.toFixed(4)}</span></div>`;
    ll.append(card);
  }
  for (const r of $$('#lens-list input[type=range]')) {
    r.oninput = e => {
      const v = parseFloat(e.target.value);
      e.target.closest('.field').querySelector('span').textContent = `${v.toFixed(2)}°`;
    };
    r.onchange = async e => {
      await api.patch(`/api/lenses/${e.target.dataset.lens}`, { fov: parseFloat(e.target.value) });
      await reload();
      schedulePreview();
    };
  }

  // 선택한 사진
  const info = $('#sel-info');
  if (S.selected != null && p.images[S.selected]) {
    const im = p.images[S.selected];
    const ex = im.exif || {};
    const par = p.params[S.selected];
    const deg = r => (r * 180 / Math.PI).toFixed(2);
    info.innerHTML = `
      <div class="meta__row"><span>${im.name}</span><span>${im.width}×${im.height}</span></div>
      ${ex.model ? `<div class="meta__row"><span>카메라</span><span>${ex.model}</span></div>` : ''}
      ${ex.lens ? `<div class="meta__row"><span>렌즈</span><span>${ex.lens}</span></div>` : ''}
      ${ex.focal_length ? `<div class="meta__row"><span>초점거리</span><span>${ex.focal_length}mm</span></div>` : ''}
      <div class="meta__row"><span>추정 화각</span><span>${im.fov_hint.toFixed(1)}°</span></div>
      ${par ? `<div class="meta__row"><span>yaw / pitch / roll</span><span>${deg(par.yaw)} / ${deg(par.pitch)} / ${deg(par.roll)}</span></div>` : ''}`;
    $('#fld-ev').hidden = false;
    const ev = p.exposure_ev?.[S.selected] ?? 0;
    $('#rng-ev').value = ev;
    $('#lbl-ev').textContent = `${ev >= 0 ? '+' : ''}${Number(ev).toFixed(2)} EV`;
  } else {
    info.textContent = '사진을 클릭하면 정보가 나옵니다';
    $('#fld-ev').hidden = true;
  }

  // 출력 크기
  const os = $('#out-size');
  if (S.preview) {
    const [w, h] = S.preview.full_size;
    const [nw, nh] = S.preview.native_size || [w, h];
    os.innerHTML = `<b>${w.toLocaleString()} × ${h.toLocaleString()}</b><br>${S.preview.megapixels.toLocaleString()} 메가픽셀`;
    $('#lbl-native').textContent = `원본 ${nw.toLocaleString()}×${nh.toLocaleString()}`;
    if (document.activeElement !== $('#num-width') && document.activeElement !== $('#num-height')) {
      $('#num-width').value = w;
      $('#num-height').value = h;
    }
    S.aspect = nw / nh;
  } else {
    os.textContent = '정렬 후 계산됩니다';
  }
}

/* ------------------------------------------------------------ 미리보기 */

let panoView;
let previewTimer = null;

function schedulePreview(delay = 260) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => refreshPreview(), delay);
}

async function refreshPreview() {
  const p = S.project;
  if (!p || !Object.keys(p.images).length) return;
  if (S.busy) return;
  const body = { max_dim: 1600, show_seams: $('#chk-seams').checked };
  try {
    const r = await job('미리보기', () => api.post('/api/preview', body));
    if (!r) return;
    S.preview = r;
    $('#preview-empty').hidden = true;
    await panoView.show(r.url + '?t=' + Date.now(), r);
    paintStatus();
    paintPanel();
  } catch (e) {
    toast(`미리보기 실패: ${e.message}`, 'bad');
  }
}

/* ------------------------------------------------------------ 제어점 편집기 */

let paneA, paneB;

function paintCPSelectors() {
  const p = S.project;
  const ids = Object.keys(p.images).map(Number).sort((a, b) => a - b);
  for (const [sel, slot] of [[$('#cp-a'), 0], [$('#cp-b'), 1]]) {
    const cur = S.cpPair[slot] ?? ids[slot] ?? null;
    sel.innerHTML = ids.map(i => `<option value="${i}">${i + 1}. ${p.images[i].name}</option>`).join('');
    if (cur != null && ids.includes(cur)) sel.value = cur;
    S.cpPair[slot] = Number(sel.value);
  }
}

async function loadCPPair() {
  const [a, b] = S.cpPair;
  if (a == null || b == null || a === b) return;
  const p = S.project;
  await paneA.load(`/api/proxy/${a}`, [p.images[a].width, p.images[a].height]);
  await paneB.load(`/api/proxy/${b}`, [p.images[b].width, p.images[b].height]);
  await refreshCPPoints();
}

async function refreshCPPoints() {
  const [a, b] = S.cpPair;
  const r = await api.get(`/api/control-points?pair=${a},${b}`);
  S.cpPoints = r.points;
  paneA.points = r.points.map(c => ({
    x: c.img_a === a ? c.xa : c.xb, y: c.img_a === a ? c.ya : c.yb,
    index: c.index, error: c.error, enabled: c.enabled }));
  paneB.points = r.points.map(c => ({
    x: c.img_a === a ? c.xb : c.xa, y: c.img_a === a ? c.yb : c.ya,
    index: c.index, error: c.error, enabled: c.enabled }));
  paneA.draw(); paneB.draw();

  const errs = r.points.filter(c => c.enabled && c.error > 0).map(c => c.error);
  const rms = errs.length ? Math.sqrt(errs.reduce((s, e) => s + e * e, 0) / errs.length) : 0;
  $('#cp-count').innerHTML = `이 쌍의 제어점 <b>${r.points.length}</b>개` +
    (errs.length ? ` · RMS <b>${rms.toFixed(2)}px</b> · 최대 <b>${Math.max(...errs).toFixed(2)}px</b>` : '');
}

async function onPaneClick(side, x, y, alt) {
  const [a, b] = S.cpPair;
  const kind = $('#cp-kind').value;

  if (kind !== 'manual') {
    // 수직·수평선은 같은 이미지 안의 두 점을 잇는다
    if (!S.pendingA) {
      S.pendingA = { side, x, y };
      $('#cp-hint').textContent = '같은 이미지에서 두 번째 점을 찍으세요';
      return;
    }
    if (S.pendingA.side !== side) { toast('같은 이미지 안에서 두 점을 찍어야 합니다', 'bad'); S.pendingA = null; return; }
    const img = side === 'a' ? a : b;
    await api.post('/api/control-points', {
      img_a: img, img_b: img, xa: S.pendingA.x, ya: S.pendingA.y, xb: x, yb: y, kind });
    S.pendingA = null;
    $('#cp-hint').textContent = `${kind === 'vertical' ? '수직선' : '수평선'} 추가됨`;
    await refreshCPPoints(); await reload();
    return;
  }

  if (side === 'a') {
    if (alt) { S.pendingA = { x, y }; $('#cp-hint').textContent = '오른쪽에서 짝이 될 지점을 클릭하세요'; paneA.points.push({ x, y, index: -1, error: 0, enabled: true }); paneA.draw(); return; }
    try {
      const r = await api.post('/api/control-points/suggest', { img_a: a, img_b: b, xa: x, ya: y });
      if (r.score < 0.45) {
        S.pendingA = { x, y };
        $('#cp-hint').textContent = `자신 없습니다(${r.score.toFixed(2)}). 오른쪽에서 직접 찍어 주세요`;
        paneA.points.push({ x, y, index: -1, error: 0, enabled: true }); paneA.draw();
        paneB.centerOn(r.xb, r.yb, Math.max(paneB.zoom, 1.5));
        return;
      }
      await api.post('/api/control-points', { img_a: a, img_b: b, xa: x, ya: y, xb: r.xb, yb: r.yb });
      $('#cp-hint').textContent = `추가됨 (일치도 ${r.score.toFixed(2)})`;
      await refreshCPPoints(); await reload();
    } catch (e) {
      toast(e.message, 'bad');
    }
  } else {
    if (!S.pendingA) { $('#cp-hint').textContent = '왼쪽 이미지를 먼저 클릭하세요'; return; }
    await api.post('/api/control-points', { img_a: a, img_b: b, xa: S.pendingA.x, ya: S.pendingA.y, xb: x, yb: y });
    S.pendingA = null;
    $('#cp-hint').textContent = '추가됨';
    await refreshCPPoints(); await reload();
  }
}

function paintCPTable() {
  const tb = $('#cp-table tbody');
  const p = S.project;
  tb.innerHTML = '';
  const rows = p.control_points
    .map((c, i) => ({ ...c, index: i }))
    .sort((x, y) => y.error - x.error);
  for (const c of rows) {
    const tr = document.createElement('tr');
    if (!c.enabled) tr.className = 'off';
    const ec = c.error > 12 ? 'err-hi' : c.error > 5 ? 'err-mid' : '';
    const kindLabel = { auto: '자동', manual: '수동', vertical: '수직선', horizontal: '수평선' }[c.kind] || c.kind;
    tr.innerHTML = `
      <td>${c.index}</td>
      <td>${(p.images[c.img_a]?.name) || c.img_a}</td>
      <td>${(p.images[c.img_b]?.name) || c.img_b}</td>
      <td>${kindLabel}</td>
      <td class="num ${ec}">${c.error ? c.error.toFixed(2) : '—'}</td>
      <td><input type="checkbox" ${c.enabled ? 'checked' : ''} data-i="${c.index}"></td>
      <td><button class="mini" data-del="${c.index}">삭제</button></td>`;
    tr.onclick = e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'BUTTON') return;
      S.cpPair = [c.img_a, c.img_b];
      paintCPSelectors();
      switchView('cp');
      loadCPPair();
    };
    tb.append(tr);
  }
  for (const cb of $$('#cp-table input[type=checkbox]'))
    cb.onchange = async e => {
      await api.patch(`/api/control-points/${e.target.dataset.i}`, { enabled: e.target.checked });
      await reload(); paintCPTable();
    };
  for (const b of $$('#cp-table button[data-del]'))
    b.onclick = async e => {
      await api.del(`/api/control-points/${e.target.dataset.del}`);
      await reload(); paintCPTable();
    };
}

/* ------------------------------------------------------------ 탭 */

function switchView(v) {
  S.view = v;
  for (const t of $$('.tab')) t.classList.toggle('tab--on', t.dataset.view === v);
  for (const el of $$('.view')) el.classList.remove('view--on');
  $(`#view-${v}`).classList.add('view--on');
  $('#seam-toggle-wrap').style.visibility = v === 'preview' ? '' : 'hidden';
  if (v === 'cp') loadCPPair();
  if (v === 'cplist') paintCPTable();
}

/* ------------------------------------------------------------ 동작 */

async function addImages(browse) {
  const r = await job('사진 불러오는 중', () => api.post('/api/images/add', { browse }));
  await reload();
  if (r?.added?.length) {
    toast(`${r.added.length}장 추가했습니다`, 'good');
    if (S.selected == null) S.selected = Object.keys(S.project.images).map(Number)[0] ?? null;
    paintStrip(); paintPanel();
  }
}

async function doAlign() {
  const n = Object.keys(S.project.images).length;
  if (n < 2) return toast('사진이 두 장 이상 필요합니다', 'bad');
  try {
    const r = await job('자동 정렬', () => api.post('/api/align', { detect: true, mode: 'full', straighten: true }));
    await reload();
    if (r) {
      const groups = r.groups || [];
      if (groups.length > 1) {
        toast(`정렬했지만 ${groups.length}조각으로 끊겼습니다 (${groups.join(', ')}장). 제어점 탭에서 이어 주세요`, 'bad');
      } else {
        toast(`정렬 완료 — RMS ${r.rms.toFixed(2)}px, 제어점 ${r.control_points}개`, 'good');
      }
      await refreshPreview();
    }
  } catch (e) {
    toast(`정렬 실패: ${e.message}`, 'bad');
  }
}

async function doOptimize(mode) {
  try {
    const r = await job('최적화', () => api.post('/api/optimize', { mode }));
    await reload();
    if (r) toast(`RMS ${r.rms.toFixed(2)}px (최대 ${r.max_error.toFixed(1)}px)`, r.rms < 8 ? 'good' : '');
    await refreshPreview();
  } catch (e) { toast(`최적화 실패: ${e.message}`, 'bad'); }
}

async function doRender() {
  if (!S.project.optimized) return toast('먼저 자동 정렬을 해주세요', 'bad');
  const [w, h] = S.preview?.full_size || [0, 0];
  if (!confirm(`${w.toLocaleString()} × ${h.toLocaleString()} 픽셀로 내보냅니다.\n바탕화면에 저장됩니다. 계속할까요?`)) return;
  try {
    const r = await job('최종 렌더링', () => api.post('/api/render', {}));
    if (r) {
      toast(`저장 완료 — ${r.width}×${r.height}, ${Math.round(r.seconds)}초`, 'good');
      await api.post('/api/reveal', { path: r.path });
    }
  } catch (e) { toast(`렌더 실패: ${e.message}`, 'bad'); }
}

async function patchSettings(patch, repreview = true) {
  S.project.settings = await api.patch('/api/settings', patch);
  await reload();
  if (repreview) schedulePreview(60);
}

/* ------------------------------------------------------------ 초기화 */

function bind() {
  $('#btn-add').onclick = () => addImages('files');
  $('#btn-add-folder').onclick = () => addImages('folder');
  $('#btn-align').onclick = doAlign;
  $('#btn-optimize').onclick = () => doOptimize('full');
  $('#btn-straighten').onclick = async () => { await api.post('/api/straighten'); await reload(); schedulePreview(0); };
  $('#btn-render').onclick = doRender;
  $('#btn-refresh').onclick = () => refreshPreview();

  $('#btn-save').onclick = async () => {
    const r = await api.post('/api/project/save', {});
    toast(`저장했습니다 — ${r.path.split('/').pop()}`, 'good');
  };
  $('#btn-open').onclick = async () => {
    const r = await api.post('/api/project/open', {});
    if (r?.cancelled) return;
    S.project = r; S.preview = null; S.selected = null;
    paintAll(); schedulePreview(0);
  };

  for (const t of $$('.tab')) t.onclick = () => switchView(t.dataset.view);

  for (const b of $$('#seg-proj .seg__b, #seg-proj2 .seg__b'))
    b.onclick = () => patchSettings({ projection: b.dataset.v });

  // 리틀 플래닛 / 터널 — 스테레오 투영의 중심을 바닥이나 하늘로 돌린다.
  // 내 좌표계는 Y 가 아래라서, 바닥을 중앙에 두려면 중심 pitch 가 -90 이다.
  const planetPreset = async (pitch, label) => {
    await api.patch('/api/settings', { projection: 'stereographic' });
    const r = await job(label, () => api.post('/api/preview', {
      max_dim: 1600, show_seams: $('#chk-seams').checked,
      center_yaw: 0, center_pitch: pitch }));
    if (r) { S.preview = r; await panoView.show(r.url + '?t=' + Date.now(), r); }
    await reload();
  };
  $('#btn-planet').onclick = () => planetPreset(-90, '리틀 플래닛');
  $('#btn-tunnel').onclick = () => planetPreset(90, '터널');

  // 출력 픽셀 직접 입력 — 한쪽을 고치면 다른 쪽은 비율로 따라간다
  const applyWidth = async (w) => {
    if (!w || w < 64) return;
    await patchSettings({ out_width: Math.round(w), scale_percent: 100 }, true);
  };
  $('#num-width').onchange = e => applyWidth(parseInt(e.target.value, 10));
  $('#num-height').onchange = e => {
    const h = parseInt(e.target.value, 10);
    if (h && S.aspect) applyWidth(Math.round(h * S.aspect));
  };
  $('#num-width').oninput = e => {
    const w = parseInt(e.target.value, 10);
    if (w && S.aspect) $('#num-height').value = Math.round(w / S.aspect);
  };
  $('#num-height').oninput = e => {
    const h = parseInt(e.target.value, 10);
    if (h && S.aspect) $('#num-width').value = Math.round(h * S.aspect);
  };
  $('#btn-px-reset').onclick = () => patchSettings({ out_width: 0, scale_percent: 100 });

  $('#sel-seam').onchange   = e => patchSettings({ seam: e.target.value });
  $('#sel-blend').onchange  = e => patchSettings({ blender: e.target.value });
  $('#sel-exposure').onchange = e => patchSettings({ exposure: e.target.value });
  $('#chk-wb').onchange     = e => patchSettings({ per_channel: e.target.checked });
  $('#chk-vig').onchange    = e => patchSettings({ vignetting: e.target.checked });
  $('#rng-lf').oninput      = e => $('#lbl-lf').textContent = `${e.target.value}%`;
  $('#rng-lf').onchange     = e => patchSettings({ low_freq: +e.target.value / 100 });
  $('#sel-format').onchange = e => patchSettings({ format: e.target.value }, false);
  $('#rng-quality').oninput = e => $('#lbl-quality').textContent = e.target.value;
  $('#rng-quality').onchange = e => patchSettings({ quality: +e.target.value }, false);
  $('#rng-scale').oninput   = e => $('#lbl-scale').textContent = `${e.target.value}%`;
  $('#rng-scale').onchange  = e => patchSettings({ scale_percent: +e.target.value, out_width: 0 }, true);
  $('#chk-seams').onchange  = () => refreshPreview();

  for (const [rng, lbl, key] of [['#rng-yaw', '#lbl-yaw', 'center_yaw'], ['#rng-pitch', '#lbl-pitch', 'center_pitch']]) {
    $(rng).oninput = e => $(lbl).textContent = `${Math.round(e.target.value)}°`;
    $(rng).onchange = async e => {
      const body = { max_dim: 1600, show_seams: $('#chk-seams').checked };
      body[key === 'center_yaw' ? 'center_yaw' : 'center_pitch'] = parseFloat(e.target.value);
      const r = await job('미리보기', () => api.post('/api/preview', body));
      if (r) { S.preview = r; await panoView.show(r.url + '?t=' + Date.now(), r); await reload(); }
    };
  }

  $('#rng-ev').oninput = e => $('#lbl-ev').textContent = `${e.target.value >= 0 ? '+' : ''}${(+e.target.value).toFixed(2)} EV`;
  $('#rng-ev').onchange = async e => {
    if (S.selected == null) return;
    S.project = await api.patch(`/api/images/${S.selected}`, { exposure_ev: parseFloat(e.target.value) });
    schedulePreview(60);
  };

  // 제어점 탭
  $('#cp-a').onchange = e => { S.cpPair[0] = +e.target.value; loadCPPair(); };
  $('#cp-b').onchange = e => { S.cpPair[1] = +e.target.value; loadCPPair(); };
  $('#cp-swap').onclick = () => {
    S.cpPair = [S.cpPair[1], S.cpPair[0]];
    paintCPSelectors(); loadCPPair();
  };
  $('#cp-prune').onclick = async () => {
    const r = await api.post('/api/control-points/prune', { threshold: 0 });
    toast(r.disabled ? `잔차 ${r.threshold.toFixed(1)}px 초과 ${r.disabled}개를 껐습니다` : '정리할 점이 없습니다');
    await reload(); await refreshCPPoints();
    if (r.disabled) doOptimize('full');
  };

  // 뷰바
  $('#zoom-in').onclick  = () => panoView.setZoom(panoView.zoom * 1.35);
  $('#zoom-out').onclick = () => panoView.setZoom(panoView.zoom / 1.35);
  $('#zoom-fit').onclick = () => panoView.fit();
  $('#zoom-100').onclick = () => panoView.setZoom(1);

  // 드래그&드롭
  let dragDepth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', e => {
    if (!hasFiles(e)) return;         // 텍스트나 이미지 드래그에는 반응하지 않는다
    e.preventDefault();
    if (++dragDepth === 1) $('#drop').hidden = false;
  });
  window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('dragleave', e => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    if (--dragDepth <= 0) { dragDepth = 0; $('#drop').hidden = true; }
  });
  window.addEventListener('drop', async e => {
    e.preventDefault(); dragDepth = 0; $('#drop').hidden = true;
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    progressOn('사진 받는 중');
    try {
      const r = await api.upload(files);
      await reload();
      toast(`${r.added.length}장 추가했습니다`, 'good');
    } catch (err) { toast(err.message, 'bad'); }
    finally { progressOff(); }
  });

  // 단축키
  window.addEventListener('keydown', e => {
    const typing = /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName || '');
    if (typing) return;
    const meta = e.metaKey || e.ctrlKey;
    if (meta && e.key === 's') { e.preventDefault(); $('#btn-save').click(); }
    else if (meta && e.key === 'o') { e.preventDefault(); $('#btn-open').click(); }
    else if (meta && e.shiftKey && e.key.toLowerCase() === 'e') { e.preventDefault(); doRender(); }
    else if (meta && e.key === 'Enter') { e.preventDefault(); doAlign(); }
    else if (e.key === 'a' && !meta) addImages('files');
    else if (e.key === 'r' && !meta) refreshPreview();
    else if (e.key === 'f' && !meta) panoView.fit();
    else if (e.key === '1') switchView('preview');
    else if (e.key === '2') switchView('cp');
    else if (e.key === '3') switchView('cplist');
    else if ((e.key === 'Backspace' || e.key === 'Delete') && S.view === 'cp') {
      const sel = paneA.sel >= 0 ? paneA.points[paneA.sel] : (paneB.sel >= 0 ? paneB.points[paneB.sel] : null);
      if (sel && sel.index >= 0) {
        api.del(`/api/control-points/${sel.index}`).then(async () => {
          paneA.sel = paneB.sel = -1;
          await reload(); await refreshCPPoints();
        });
      }
    }
  });
}

async function main() {
  panoView = new PanoView($('#pano'), $('#canvas-wrap'), (z, fit) => {
    $('#zoom-label').textContent = Math.abs(z - fit) < 1e-6 ? '맞춤' : `${Math.round(z * 100)}%`;
    if (S.preview) {
      const l = S.preview.layout;
      $('#view-info').textContent =
        `미리보기 ${S.preview.width}×${S.preview.height} · 출력 ${S.preview.full_size[0]}×${S.preview.full_size[1]} (${S.preview.megapixels}MP)`;
    }
  });

  const mk = (cvId, paneSel, side) => new CPPane($(cvId), $(paneSel), {
    loupe: $(`#loupe-${side}`), loupeCv: $(`#loupe-canvas-${side}`),
    onClick: (x, y, alt) => onPaneClick(side, x, y, alt),
    onDragEnd: async (pt) => {
      const payload = side === 'a' ? { xa: pt.x, ya: pt.y } : { xb: pt.x, yb: pt.y };
      await api.patch(`/api/control-points/${pt.index}`, payload);
      await refreshCPPoints();
    },
    onSelect: () => { if (side === 'a') paneB.sel = paneA.sel; else paneA.sel = paneB.sel; paneA.draw(); paneB.draw(); },
  });
  paneA = mk('#cp-canvas-a', '.cp-pane[data-side=a]', 'a');
  paneB = mk('#cp-canvas-b', '.cp-pane[data-side=b]', 'b');

  bind();
  await reload();
  if (Object.keys(S.project.images).length && S.project.optimized) refreshPreview();
}

main().catch(e => { console.error(e); toast(`시작 실패: ${e.message}`, 'bad'); });

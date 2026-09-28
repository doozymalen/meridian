// 캔버스 뷰 두 가지 — 파노라마 미리보기와 제어점 편집기.

/* ------------------------------------------------------------ 미리보기 */

export class PanoView {
  constructor(canvas, wrap, onInfo) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.wrap = wrap;
    this.onInfo = onInfo;
    this.img = null;
    this.zoom = 1;
    this.fitZoom = 1;
    this.ox = 0;
    this.oy = 0;
    this._bind();
  }

  _bind() {
    let drag = null;
    this.wrap.addEventListener('pointerdown', e => {
      if (!this.img) return;
      drag = { x: e.clientX, y: e.clientY, ox: this.ox, oy: this.oy };
      this.wrap.setPointerCapture(e.pointerId);
    });
    this.wrap.addEventListener('pointermove', e => {
      if (!drag) return;
      this.ox = drag.ox + (e.clientX - drag.x);
      this.oy = drag.oy + (e.clientY - drag.y);
      this._apply();
    });
    const end = e => {
      if (drag) { drag = null; try { this.wrap.releasePointerCapture(e.pointerId); } catch {} }
    };
    this.wrap.addEventListener('pointerup', end);
    this.wrap.addEventListener('pointercancel', end);

    this.wrap.addEventListener('wheel', e => {
      if (!this.img) return;
      e.preventDefault();
      const r = this.wrap.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      // 커서 아래 지점을 고정한 채 확대한다
      const k = Math.exp(-e.deltaY * 0.0016);
      const next = Math.min(12, Math.max(this.fitZoom * 0.3, this.zoom * k));
      const s = next / this.zoom;
      this.ox = mx - (mx - this.ox) * s;
      this.oy = my - (my - this.oy) * s;
      this.zoom = next;
      this._apply();
    }, { passive: false });

    new ResizeObserver(() => this.img && this._apply()).observe(this.wrap);
  }

  async show(url, meta) {
    const img = new Image();
    await new Promise((ok, no) => { img.onload = ok; img.onerror = no; img.src = url; });
    this.img = img;
    this.meta = meta;
    this.cv.width = img.naturalWidth;
    this.cv.height = img.naturalHeight;
    this.ctx.drawImage(img, 0, 0);
    this.fit();
  }

  fit() {
    if (!this.img) return;
    const r = this.wrap.getBoundingClientRect();
    const pad = 18;
    this.fitZoom = Math.min((r.width - pad) / this.cv.width, (r.height - pad) / this.cv.height);
    this.zoom = this.fitZoom;
    this.ox = (r.width - this.cv.width * this.zoom) / 2;
    this.oy = (r.height - this.cv.height * this.zoom) / 2;
    this._apply();
  }

  setZoom(z) {
    if (!this.img) return;
    const r = this.wrap.getBoundingClientRect();
    const cx = r.width / 2, cy = r.height / 2;
    const s = z / this.zoom;
    this.ox = cx - (cx - this.ox) * s;
    this.oy = cy - (cy - this.oy) * s;
    this.zoom = z;
    this._apply();
  }

  _apply() {
    this.cv.style.transform = `translate(${this.ox}px, ${this.oy}px) scale(${this.zoom})`;
    this.onInfo?.(this.zoom, this.fitZoom);
  }
}

/* ------------------------------------------------------------ 제어점 편집기 */

export class CPPane {
  constructor(canvas, pane, opts) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.pane = pane;
    this.opts = opts;                 // { onClick, onPick, loupe, loupeCv }
    this.img = null;
    this.zoom = 1;
    this.fitZoom = 1;
    this.ox = 0;
    this.oy = 0;
    this.points = [];                 // [{x, y, index, error, enabled}] 원본 좌표
    this.sel = -1;
    this.hi = -1;
    this._bind();
  }

  async load(url, size) {
    const img = new Image();
    await new Promise((ok, no) => { img.onload = ok; img.onerror = no; img.src = url; });
    this.img = img;
    this.srcW = size[0];
    this.srcH = size[1];
    this.fit();
  }

  // 원본 좌표 <-> 화면 좌표
  toScreen(x, y) {
    const k = this.img.naturalWidth / this.srcW;
    return [x * k * this.zoom + this.ox, y * k * this.zoom + this.oy];
  }
  toSource(sx, sy) {
    const k = this.img.naturalWidth / this.srcW;
    return [(sx - this.ox) / this.zoom / k, (sy - this.oy) / this.zoom / k];
  }

  fit() {
    if (!this.img) return;
    const r = this.pane.getBoundingClientRect();
    this.cv.width = Math.max(1, Math.round(r.width));
    this.cv.height = Math.max(1, Math.round(r.height));
    const pad = 16;
    this.fitZoom = Math.min((r.width - pad) / this.img.naturalWidth,
                            (r.height - pad) / this.img.naturalHeight);
    this.zoom = this.fitZoom;
    this.ox = (r.width - this.img.naturalWidth * this.zoom) / 2;
    this.oy = (r.height - this.img.naturalHeight * this.zoom) / 2;
    this.draw();
  }

  centerOn(x, y, zoom) {
    if (!this.img) return;
    const r = this.pane.getBoundingClientRect();
    this.zoom = zoom ?? Math.max(this.zoom, 1.2);
    const k = this.img.naturalWidth / this.srcW;
    this.ox = r.width / 2 - x * k * this.zoom;
    this.oy = r.height / 2 - y * k * this.zoom;
    this.draw();
  }

  draw() {
    if (!this.img) return;
    const c = this.ctx;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, this.cv.width, this.cv.height);
    c.imageSmoothingEnabled = this.zoom < 2.5;
    c.drawImage(this.img, this.ox, this.oy,
                this.img.naturalWidth * this.zoom, this.img.naturalHeight * this.zoom);

    for (let i = 0; i < this.points.length; i++) {
      const p = this.points[i];
      const [sx, sy] = this.toScreen(p.x, p.y);
      if (sx < -20 || sy < -20 || sx > this.cv.width + 20 || sy > this.cv.height + 20) continue;
      const on = i === this.sel, hot = i === this.hi;
      const col = !p.enabled ? '#6b625a'
                : p.error > 12 ? '#cf6b5c'
                : p.error > 5  ? '#e0b552' : '#5fc9e8';
      c.lineWidth = on ? 2.2 : 1.4;
      c.strokeStyle = on ? '#d4874a' : col;
      c.beginPath(); c.arc(sx, sy, on || hot ? 9 : 6, 0, Math.PI * 2); c.stroke();
      c.beginPath();
      c.moveTo(sx - 12, sy); c.lineTo(sx - 4, sy);
      c.moveTo(sx + 4, sy);  c.lineTo(sx + 12, sy);
      c.moveTo(sx, sy - 12); c.lineTo(sx, sy - 4);
      c.moveTo(sx, sy + 4);  c.lineTo(sx, sy + 12);
      c.stroke();
      if (on || hot) {
        c.fillStyle = 'rgba(12,9,8,.82)';
        c.fillRect(sx + 12, sy - 20, 44, 15);
        c.fillStyle = '#ece6dd';
        c.font = '10px JetBrains Mono, monospace';
        c.fillText(`#${p.index}`, sx + 16, sy - 9);
      }
    }
  }

  hitTest(sx, sy) {
    for (let i = this.points.length - 1; i >= 0; i--) {
      const [px, py] = this.toScreen(this.points[i].x, this.points[i].y);
      if (Math.hypot(px - sx, py - sy) < 11) return i;
    }
    return -1;
  }

  _bind() {
    let drag = null, moved = false;

    this.pane.addEventListener('pointerdown', e => {
      if (!this.img) return;
      const r = this.pane.getBoundingClientRect();
      const sx = e.clientX - r.left, sy = e.clientY - r.top;
      moved = false;
      if (e.button === 1 || e.shiftKey) {            // 휠클릭·시프트 = 이동
        drag = { x: e.clientX, y: e.clientY, ox: this.ox, oy: this.oy, pan: true };
      } else {
        const hit = this.hitTest(sx, sy);
        if (hit >= 0) {
          this.sel = hit;
          this.opts.onSelect?.(this.points[hit]);
          this.draw();
          drag = { pan: false, hit };
        } else {
          drag = { x: e.clientX, y: e.clientY, ox: this.ox, oy: this.oy, pan: true, maybeClick: [sx, sy], alt: e.altKey };
        }
      }
      this.pane.setPointerCapture(e.pointerId);
    });

    this.pane.addEventListener('pointermove', e => {
      const r = this.pane.getBoundingClientRect();
      const sx = e.clientX - r.left, sy = e.clientY - r.top;
      if (drag?.pan) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (Math.hypot(dx, dy) > 3) moved = true;
        this.ox = drag.ox + dx;
        this.oy = drag.oy + dy;
        this.draw();
      } else if (drag && drag.hit >= 0) {
        const [x, y] = this.toSource(sx, sy);
        this.points[drag.hit].x = x;
        this.points[drag.hit].y = y;
        moved = true;
        this.draw();
        this._loupe(sx, sy);
      } else if (!drag) {
        const h = this.hitTest(sx, sy);
        if (h !== this.hi) { this.hi = h; this.draw(); }
        this._loupe(sx, sy);
      }
    });

    this.pane.addEventListener('pointerleave', () => {
      this.opts.loupe?.classList.remove('loupe--on');
      if (this.hi !== -1) { this.hi = -1; this.draw(); }
    });

    this.pane.addEventListener('pointerup', e => {
      const r = this.pane.getBoundingClientRect();
      const sx = e.clientX - r.left, sy = e.clientY - r.top;
      if (drag?.maybeClick && !moved) {
        const [x, y] = this.toSource(sx, sy);
        if (x >= 0 && y >= 0 && x < this.srcW && y < this.srcH) {
          this.opts.onClick?.(x, y, drag.alt);
        }
      } else if (drag && drag.hit >= 0 && moved) {
        this.opts.onDragEnd?.(this.points[drag.hit]);
      }
      drag = null;
      try { this.pane.releasePointerCapture(e.pointerId); } catch {}
    });

    this.pane.addEventListener('wheel', e => {
      if (!this.img) return;
      e.preventDefault();
      const r = this.pane.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const k = Math.exp(-e.deltaY * 0.0016);
      const next = Math.min(16, Math.max(this.fitZoom * 0.5, this.zoom * k));
      const s = next / this.zoom;
      this.ox = mx - (mx - this.ox) * s;
      this.oy = my - (my - this.oy) * s;
      this.zoom = next;
      this.draw();
      this.opts.onZoom?.(this.zoom);
    }, { passive: false });

    new ResizeObserver(() => this.img && this.fit()).observe(this.pane);
  }

  // 커서 주변을 크게 보여주는 확대경 — 제어점을 정확히 찍는 데 꼭 필요하다
  _loupe(sx, sy) {
    const el = this.opts.loupe, lc = this.opts.loupeCv;
    if (!el || !lc || !this.img) return;
    const R = 128, MAG = 5;
    lc.width = R; lc.height = R;
    const g = lc.getContext('2d');
    const k = this.img.naturalWidth / this.srcW;
    const [ix, iy] = this.toSource(sx, sy);
    const px = ix * k, py = iy * k;
    const half = R / (2 * MAG);
    g.imageSmoothingEnabled = false;
    g.fillStyle = '#0d0b0a';
    g.fillRect(0, 0, R, R);
    g.drawImage(this.img, px - half, py - half, half * 2, half * 2, 0, 0, R, R);
    g.strokeStyle = 'rgba(212,135,74,.9)';
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(R / 2, R / 2 - 11); g.lineTo(R / 2, R / 2 - 3);
    g.moveTo(R / 2, R / 2 + 3);  g.lineTo(R / 2, R / 2 + 11);
    g.moveTo(R / 2 - 11, R / 2); g.lineTo(R / 2 - 3, R / 2);
    g.moveTo(R / 2 + 3, R / 2);  g.lineTo(R / 2 + 11, R / 2);
    g.stroke();
    const r = this.pane.getBoundingClientRect();
    el.style.left = `${Math.min(r.width - R - 8, Math.max(8, sx + 20))}px`;
    el.style.top  = `${Math.min(r.height - R - 8, Math.max(8, sy - R - 16))}px`;
    el.classList.add('loupe--on');
  }
}

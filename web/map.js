/* 지도 전용 프론트. 데이터 계약: docs/map-frontend-contract.md */
(() => {
  'use strict';
  let sdkPromise;
  function loadSDK() {
    if (window.Tmapv2?.Map) return Promise.resolve(window.Tmapv2);
    if (sdkPromise) return sdkPromise;
    const key = window.VOLTGO_MAP_CONFIG?.appKey?.trim();
    if (!key) return window.loadVoltGoOpenMap();
    sdkPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://apis.openapi.sk.com/tmap/jsv2?version=1&appKey=' + encodeURIComponent(key);
      script.async = true;
      const finish = (error) => {
        clearInterval(poll); clearTimeout(timeout);
        script.onerror = null;
        if (error) { script.remove(); reject(error); }
        else resolve(window.Tmapv2);
      };
      const poll = setInterval(() => { if (window.Tmapv2?.Map) finish(); }, 100);
      const timeout = setTimeout(() => finish(new Error('지도를 불러오지 못했어요. 다시 시도해 주세요.')), 15000);
      script.onerror = () => finish(new Error('지도 연결이 원활하지 않아요. 다시 시도해 주세요.'));
      document.head.append(script);
    }).catch((error) => { sdkPromise = null; throw error; });
    return sdkPromise;
  }

  const validPair = (p) => Array.isArray(p) && p.length >= 2 &&
    typeof p[0] === 'number' && typeof p[1] === 'number' &&
    Number.isFinite(p[0]) && Number.isFinite(p[1]) && Math.abs(p[0]) <= 180 && Math.abs(p[1]) <= 90;
  const point = (p) => p && validPair([p.longitude, p.latitude]) ? [p.longitude, p.latitude] : null;
  // 잘못된 점을 건너뛰어 도로를 직결하지 않는다. 한 점이라도 잘못되면 해당 구간을 제외한다.
  function segments(geometry) {
    const lines = geometry?.type === 'LineString' ? [geometry.coordinates] :
      geometry?.type === 'MultiLineString' ? geometry.coordinates : [];
    return Array.isArray(lines) ? lines.filter((line) => Array.isArray(line) && line.length >= 2 && line.every(validPair)) : [];
  }
  function icon(label, color) {
    return 'data:image/svg+xml;charset=UTF-8,' + encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="40" viewBox="0 0 32 40"><path d="M16 39 5 25A15 15 0 1 1 27 25Z" fill="${color}" stroke="white" stroke-width="2"/><text x="16" y="22" text-anchor="middle" font-family="sans-serif" font-size="14" font-weight="bold" fill="white">${label}</text></svg>`);
  }

  class RouteMap {
    constructor(panel) {
      this.panel = panel;
      this.entries = new Map();
      this.counter = 0;
      this.revision = 0;
      this.overlays = [];
      this.direction = 'both';
      this.canvas = panel.querySelector('.map-canvas');
      this.resizeObserver = new ResizeObserver(() => {
        if (this.map && !this.panel.hidden && !this.canvas.hidden) {
          this.map.resize('100%', '100%');
          this.fit();
        }
      });
      this.resizeObserver.observe(panel.querySelector('.map-stage'));
      panel.querySelector('.map-retry').addEventListener('click', () => this.render());
      panel.querySelector('.map-fit').addEventListener('click', () => this.fit());
      panel.querySelector('.map-tile-retry').addEventListener('click', () => {
        panel.querySelector('.map-tile-status').hidden = true;
        this.map?.retryTiles?.();
      });
      panel.querySelectorAll('[data-direction]').forEach((button) => button.addEventListener('click', () => {
        this.direction = button.dataset.direction;
        this.render();
      }));
    }

    register(response, data) {
      const candidates = response?.candidates || [];
      const snapshot = data && typeof data === 'object' ? data : {};
      // Labels belong to this recommendation snapshot. A confirmed B stays B.
      const labels = candidates.map((candidate, index) => {
        const previous = response.status === 'confirmed' && [...this.entries.values()].reverse().find(entry =>
          entry.candidate.plan_id === candidate.plan_id && entry.candidate.version === candidate.version);
        return previous?.label || String.fromCharCode(65 + index);
      });
      return candidates.map((candidate, index) => {
        const key = 'map-' + (++this.counter);
        this.entries.set(key, { candidate, candidates, data: snapshot, label: labels[index], labels });
        return key;
      });
    }

    select(key, scroll = false, record = true) {
      if (!this.entries.has(key)) return;
      this.selected = key;
      this.panel.hidden = false;
      document.querySelectorAll('[data-map-key]').forEach((button) => {
        const active = button.dataset.mapKey === key;
        button.setAttribute('aria-pressed', String(active));
        button.closest('.plan')?.classList.toggle('map-selected', active);
      });
      this.render();
      if (record) this.onSelect?.();
      if (scroll) this.panel.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
    }

    clearOverlays() {
      this.overlays.forEach((overlay) => overlay.setMap(null));
      this.overlays = [];
      this.fitPoints = [];
    }

    message(text, retry = false) {
      this.panel.querySelector('.map-empty').hidden = false;
      this.panel.querySelector('.map-empty p').textContent = text;
      this.panel.querySelector('.map-retry').hidden = !retry;
      this.canvas.hidden = true;
      this.panel.querySelector('.map-fit').disabled = true;
      this.panel.querySelector('.map-fit').hidden = true;
      this.panel.querySelector('.map-controls').hidden = true;
      this.panel.querySelector('.map-tile-status').hidden = true;
    }

    overview(data) {
      this.overviewData = data || {};
      this.selected = null;
      this.panel.hidden = false;
      this.render();
    }

    async render() {
      const revision = ++this.revision;
      const entry = this.entries.get(this.selected);
      if (!entry && !this.overviewData) return;
      this.clearOverlays();
      const { candidate, candidates, data, label, labels } = entry || {candidate: null, candidates: [], data: this.overviewData, label: '', labels: []};
      this.panel.querySelector('.map-start').hidden = Boolean(entry);
      const choices = this.panel.querySelector('.map-places');
      choices.replaceChildren();
      candidates.forEach((place, index) => {
        const match = [...this.entries].find(([, e]) => e.data === data && e.candidate === place);
        if (!match) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.mapChoice = match[0];
        button.textContent = `${labels[index]} · ${place.name}`;
        button.setAttribute('aria-pressed', String(match[0] === this.selected));
        button.addEventListener('click', () => this.select(match[0]));
        choices.append(button);
      });
      const places = Array.isArray(data.places) ? data.places : [];
      const routes = Array.isArray(data.routes) ? data.routes : [];
      const place = candidate && places.find((p) => p?.poi_id === candidate.poi_id);
      const origin = point(data.origin), destination = point(place);
      this.panel.querySelector('.map-place').textContent = candidate ? `${label} · ${candidate.name}` : data.origin?.name || '근처 추천 장소';
      this.panel.querySelector('.map-note').textContent = '';
      this.panel.querySelectorAll('[data-direction]').forEach((button) => {
        button.setAttribute('aria-pressed', String(button.dataset.direction === this.direction));
      });
      if (!origin && !destination) {
        this.message(candidate ? '장소 위치를 아직 받지 못했어요. 위치가 준비되면 지도에서 볼 수 있어요.' : '출발 충전소를 알려 주세요. 위치를 확인하면 주변 지도를 보여드릴게요.');
        return;
      }
      this.message('지도를 불러오는 중이에요…');
      try {
        const T = await loadSDK();
        if (revision !== this.revision) return;
        this.sdk = T;
        this.canvas.hidden = false;
        if (!this.map) {
          const center = origin || destination;
          this.map = new T.Map(this.canvas.id, { center: new T.LatLng(center[1], center[0]), width: '100%', height: '100%', zoom: 16, zoomControl: true, scrollwheel: false, httpsMode: true });
          this.map.onTileStatus = text => {
            if (this.panel.hidden || this.canvas.hidden) return;
            this.panel.querySelector('.map-tile-status p').textContent = text;
            this.panel.querySelector('.map-tile-status').hidden = !text;
          };
        }
        this.map.resize('100%', '100%');
        this.panel.querySelector('.map-empty').hidden = true;
        this.panel.querySelector('.map-fit').disabled = false;
        this.panel.querySelector('.map-fit').hidden = false;
        this.panel.querySelector('.map-controls').hidden = !candidate;
        const latLng = (p) => new T.LatLng(p[1], p[0]);
        const addMarker = (coords, label, color, title, onClick) => {
          const marker = new T.Marker({ position: latLng(coords), map: this.map, icon: icon(label, color), iconSize: new T.Size(32, 40), title });
          if (onClick) marker.addListener('click', onClick);
          this.overlays.push(marker);
        };
        if (origin) { addMarker(origin, '⚡', '#1B2620', '출발 충전소'); this.fitPoints.push(origin); }
        if (!candidate) {
          this.panel.querySelector('.map-note').textContent = '출발 충전소 주변이에요. 장소를 추천받으면 지도에서 함께 볼 수 있어요.';
          this.fit();
          return;
        }
        candidates.forEach((c, index) => {
          const p = places.find((p) => p?.poi_id === c.poi_id), coords = point(p);
          if (!coords) return;
          const selected = c.plan_id === candidate.plan_id;
          addMarker(coords, labels[index], selected ? '#2A8A57' : '#718B7C', `${labels[index]} · ${c.name}`, () => {
            const match = [...this.entries].find(([, e]) => e.data === data && e.candidate === c);
            if (match) this.select(match[0]);
          });
          if (selected) this.fitPoints.push(coords);
        });
        const route = routes.find((r) => r?.poi_id === candidate.poi_id);
        const notes = [];
        if (!origin) notes.push('충전소 위치 미제공');
        if (!destination) notes.push('선택한 장소 위치 미제공');
        for (const [direction, label, color] of [['outbound', '가는 길', '#2A8A57'], ['inbound', '오는 길', '#5369C5']]) {
          if (this.direction !== 'both' && this.direction !== direction) continue;
          const lines = segments(route?.[direction]);
          if (!lines.length) { notes.push(`${label} 경로 미제공`); continue; }
          for (const line of lines) {
            this.overlays.push(new T.Polyline({ path: line.map(latLng), map: this.map, strokeColor: color,
              strokeWeight: direction === 'outbound' ? 6 : 4, strokeStyle: direction === 'outbound' ? 'solid' : 'dash', strokeOpacity: 0.9 }));
            this.fitPoints.push(...line);
          }
        }
        this.panel.querySelector('.map-note').textContent = notes.length ? notes.join(' · ') : '충전소 ⚡에서 출발해 선택한 장소까지 다녀오는 도보 경로예요.';
        this.fit();
      } catch (error) {
        if (revision !== this.revision) return;
        this.clearOverlays();
        this.message(error.message || '지도를 불러오지 못했어요.', true);
      }
    }

    fit() {
      if (!this.map || !this.fitPoints?.length) return;
      const T = this.sdk;
      const [lon, lat] = this.fitPoints[0];
      if (this.fitPoints.every((p) => p[0] === lon && p[1] === lat)) {
        this.map.setCenter(new T.LatLng(lat, lon)); this.map.setZoom(16); return;
      }
      const bounds = new T.LatLngBounds();
      this.fitPoints.forEach((p) => bounds.extend(new T.LatLng(p[1], p[0])));
      this.map.fitBounds(bounds, 40);
    }

    hide() {
      ++this.revision;
      this.clearOverlays();
      this.panel.hidden = true;
      document.querySelectorAll('[data-map-key]').forEach((button) => {
        button.setAttribute('aria-pressed', 'false');
        button.closest('.plan')?.classList.remove('map-selected');
      });
    }

    reset() {
      this.hide();
      this.entries.clear();
      this.selected = null;
      this.overviewData = null;
      this.direction = 'both';
    }
  }
  window.VoltGoRouteMap = RouteMap;
})();

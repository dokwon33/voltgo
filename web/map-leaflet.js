/* Leaflet adapter for the small map interface used by RouteMap.
   Local library files; the browser requests only the visible OSM tiles. */
(() => {
  let ready;
  window.loadVoltGoOpenMap = () => {
    if (ready) return ready;
    ready = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      const timeout = setTimeout(() => finish(new Error('지도 파일을 불러오지 못했어요. 다시 시도해 주세요.')), 10000);
      function finish(error) {
        clearTimeout(timeout);
        script.onload = script.onerror = null;
        if (error) { script.remove(); ready = null; reject(error); return; }
        const L = window.L;
        class LatLng { constructor(lat, lng) { this.lat = lat; this.lng = lng; } }
        class Size { constructor(x, y) { this.x = x; this.y = y; } }
        class LatLngBounds {
          constructor() { this.points = []; }
          extend(p) { this.points.push(p); }
        }
        class Map {
          constructor(id, options) {
            this.native = L.map(id, {scrollWheelZoom: false, zoomControl: true}).setView(options.center, options.zoom);
            this.tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
              maxZoom: 19,
              attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>',
            });
            let loaded = 0, failed = 0;
            this.tiles.on('loading', () => { loaded = 0; failed = 0; });
            this.tiles.on('tileload', () => { loaded++; this.onTileStatus?.(''); });
            this.tiles.on('tileerror', () => { failed++; });
            this.tiles.on('load', () => {
              if (!loaded && failed) this.onTileStatus?.('배경 지도를 불러오지 못했어요. 다시 시도해 주세요.');
            });
            this.tiles.addTo(this.native);
          }
          resize() { this.native.invalidateSize({pan: false}); }
          fitBounds(bounds, padding) { this.native.fitBounds(bounds.points, {padding: [padding, padding], maxZoom: 17, animate: false}); }
          setCenter(p) { this.native.panTo(p, {animate: false}); }
          setZoom(zoom) { this.native.setZoom(zoom, {animate: false}); }
          retryTiles() { this.tiles.redraw(); }
        }
        class Overlay {
          setMap(map) { if (map) this.layer.addTo(map.native); else this.layer.remove(); }
          addListener(name, callback) { this.layer.on(name, callback); }
        }
        class Marker extends Overlay {
          constructor(options) {
            super();
            this.layer = L.marker(options.position, {
              title: options.title, alt: options.title,
              icon: L.icon({iconUrl: options.icon, iconSize: [32, 40], iconAnchor: [16, 40]}),
            });
            this.setMap(options.map);
          }
        }
        class Polyline extends Overlay {
          constructor(options) {
            super();
            this.layer = L.polyline(options.path, {
              color: options.strokeColor, weight: options.strokeWeight,
              opacity: options.strokeOpacity, dashArray: options.strokeStyle === 'dash' ? '8 8' : null,
            });
            this.setMap(options.map);
          }
        }
        resolve({Map, LatLng, Size, LatLngBounds, Marker, Polyline});
      }
      script.src = 'vendor/leaflet/leaflet.js';
      script.onload = () => window.L?.map ? finish() : finish(new Error('지도 파일을 읽지 못했어요. 다시 시도해 주세요.'));
      script.onerror = () => finish(new Error('지도 파일을 불러오지 못했어요. 다시 시도해 주세요.'));
      document.head.append(script);
    });
    return ready;
  };
})();

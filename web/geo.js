/*
 * Small geometry helpers for SaltScope.
 * Works in the browser (window.Geo) and in Node (require) so it can be tested.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.Geo = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var M_PER_DEG = 111320; // same constant main.py uses
  var MIN_BUFFER_M = 50;
  var MAX_BUFFER_M = 2000;
  var M2_PER_FEDDAN = 4200.83;

  function rad(deg) {
    return (deg * Math.PI) / 180;
  }

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  // Flat local projection (metres) around an origin. Fine for field-sized areas.
  function project(points, origin) {
    var k = Math.cos(rad(origin.lat));
    return points.map(function (p) {
      return { x: (p.lng - origin.lng) * k * M_PER_DEG, y: (p.lat - origin.lat) * M_PER_DEG };
    });
  }

  function unproject(point, origin) {
    var k = Math.cos(rad(origin.lat));
    return {
      lat: origin.lat + point.y / M_PER_DEG,
      lng: origin.lng + point.x / (M_PER_DEG * k)
    };
  }

  function pointInPolygon(point, ring) {
    var inside = false;
    for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      var xi = ring[i].lng, yi = ring[i].lat;
      var xj = ring[j].lng, yj = ring[j].lat;
      var crosses = (yi > point.lat) !== (yj > point.lat) &&
        point.lng < ((xj - xi) * (point.lat - yi)) / (yj - yi) + xi;
      if (crosses) inside = !inside;
    }
    return inside;
  }

  // ring: [{lat, lng}, ...] (the closing point may or may not repeat the first)
  function polygonInfo(ring) {
    var pts = ring.slice();
    if (pts.length > 1 &&
        pts[0].lat === pts[pts.length - 1].lat &&
        pts[0].lng === pts[pts.length - 1].lng) {
      pts.pop();
    }
    if (pts.length < 3) throw new Error('A shape needs at least three corners.');

    var origin = pts[0];
    var xy = project(pts, origin);
    var twiceArea = 0, cx = 0, cy = 0;
    for (var i = 0; i < xy.length; i++) {
      var j = (i + 1) % xy.length;
      var cross = xy[i].x * xy[j].y - xy[j].x * xy[i].y;
      twiceArea += cross;
      cx += (xy[i].x + xy[j].x) * cross;
      cy += (xy[i].y + xy[j].y) * cross;
    }

    var c;
    if (Math.abs(twiceArea) < 1e-6) {
      // Degenerate (a line): fall back to the average of the corners.
      c = {
        x: xy.reduce(function (s, p) { return s + p.x; }, 0) / xy.length,
        y: xy.reduce(function (s, p) { return s + p.y; }, 0) / xy.length
      };
    } else {
      c = { x: cx / (3 * twiceArea), y: cy / (3 * twiceArea) };
    }

    var center = unproject(c, origin);
    var rel = project(pts, center);
    var radiusM = 0;
    rel.forEach(function (p) {
      radiusM = Math.max(radiusM, Math.abs(p.x), Math.abs(p.y));
    });

    return {
      center: center,
      radiusM: radiusM,
      areaM2: Math.abs(twiceArea) / 2,
      outside: !pointInPolygon(center, pts)
    };
  }

  function circleInfo(center, radiusM) {
    return {
      center: { lat: center.lat, lng: center.lng },
      radiusM: radiusM,
      areaM2: Math.PI * radiusM * radiusM,
      outside: false
    };
  }

  // Half-width of the square the server scans around the center.
  function bufferFor(radiusM) {
    return {
      bufferM: clamp(Math.ceil(radiusM), MIN_BUFFER_M, MAX_BUFFER_M),
      capped: radiusM > MAX_BUFFER_M,
      tiny: radiusM < MIN_BUFFER_M
    };
  }

  // Same square main.fetch_bands requests: [[south, west], [north, east]].
  function scanBounds(lat, lng, bufferM) {
    var dlat = bufferM / M_PER_DEG;
    var dlng = bufferM / (M_PER_DEG * Math.cos(rad(lat)));
    return [[lat - dlat, lng - dlng], [lat + dlat, lng + dlng]];
  }

  function formatCoord(value) {
    return value.toFixed(6);
  }

  function formatArea(m2) {
    if (m2 < 1e4) return Math.round(m2).toLocaleString('en') + ' m²';
    var hectares = m2 / 1e4;
    var feddan = m2 / M2_PER_FEDDAN;
    var hText = hectares >= 100 ? Math.round(hectares).toLocaleString('en') : hectares.toFixed(1);
    var fText = feddan >= 100 ? Math.round(feddan).toLocaleString('en') : feddan.toFixed(1);
    return hText + ' ha (' + fText + ' feddan)';
  }

  return {
    M_PER_DEG: M_PER_DEG,
    MIN_BUFFER_M: MIN_BUFFER_M,
    MAX_BUFFER_M: MAX_BUFFER_M,
    clamp: clamp,
    pointInPolygon: pointInPolygon,
    polygonInfo: polygonInfo,
    circleInfo: circleInfo,
    bufferFor: bufferFor,
    scanBounds: scanBounds,
    formatCoord: formatCoord,
    formatArea: formatArea
  };
});

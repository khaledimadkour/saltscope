/* SaltScope – map, drawing, and salinity scan UI */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  var els = {
    notice: $('notice'), noticeText: $('noticeText'),
    stepArea: $('stepArea'), stepCenter: $('stepCenter'), stepScan: $('stepScan'),
    tools: Array.prototype.slice.call(document.querySelectorAll('.tool')),
    drawHint: $('drawHint'), clearBtn: $('clearBtn'),
    latValue: $('latValue'), lonValue: $('lonValue'),
    areaMeta: $('areaMeta'), areaNote: $('areaNote'),
    copyBtn: $('copyBtn'), copyLabel: $('copyLabel'),
    daysSelect: $('daysSelect'), startBtn: $('startBtn'), startLabel: $('startLabel'),
    refreshBtn: $('refreshBtn'),
    reading: $('reading'), spinner: $('spinner'), statusText: $('statusText'),
    dots: $('dots'), statusSub: $('statusSub'),
    stripWrap: $('stripWrap'), marker: $('marker'), statusNote: $('statusNote'),
    facts: $('facts'), relDot: $('relDot'), relText: $('relText'),
    factArea: $('factArea'), factImagery: $('factImagery'), factUpdated: $('factUpdated'),
    details: $('details'), indexRows: $('indexRows'),
    distLow: $('distLow'), distHigh: $('distHigh'), refCounts: $('refCounts'),
    basemapButtons: Array.prototype.slice.call(document.querySelectorAll('.basemap button')),
    advice: $('advice'), adviceBtn: $('adviceBtn'), adviceBtnLabel: $('adviceBtnLabel'),
    adviceStatus: $('adviceStatus'), adviceSpinner: $('adviceSpinner'),
    adviceStatusText: $('adviceStatusText'), adviceBody: $('adviceBody'),
    adviceAssessment: $('adviceAssessment'), adviceSteps: $('adviceSteps'),
    cropsContext: $('cropsContext'), adviceCrops: $('adviceCrops'),
    adviceCaution: $('adviceCaution'), adviceMeta: $('adviceMeta')
  };

  if (!window.L || !L.Draw || !window.Geo) {
    els.noticeText.textContent = 'The map libraries did not load. Reload the page.';
    els.notice.hidden = false;
    return;
  }

  var API = { health: 'api/health', scan: 'api/salinity', advice: 'api/advice' };
  var SCAN_TIMEOUT_MS = 150000;
  var ADVICE_TIMEOUT_MS = 150000;
  var INDEX_NAMES = ['NDSI', 'SI', 'SI2', 'SWIR_ND'];
  var RELIABILITY = { good: 'Good', moderate: 'Moderate', low: 'Low' };

  var HINTS = {
    idle: 'Zoom in to your field first, then pick a shape and draw it around the field.',
    done: 'Area marked. Pick a shape to draw it again, or clear the area.',
    circle: 'Press on the middle of the field, drag outward, then release.',
    polygon: 'Click each corner of the field, then click the first corner again to finish.',
    rectangle: 'Press on one corner, drag to the opposite corner, then release.'
  };

  var state = {
    phase: 'idle',       // idle | ready | loading | done | error
    area: null,          // { kind, center, radiusM, areaM2, outside, bufferM, capped, tiny }
    hasScanned: false,
    result: null,
    error: null,
    requestId: 0,
    controller: null,
    advice: { phase: 'idle', data: null, error: null, requestId: 0, controller: null },
    server: { reachable: true, credentials: true, ai: true }
  };

  /* ---------------- map ---------------- */

  // Leaflet.draw 1.0.4 has a bug in its area formatter with newer Leaflet; replace it.
  L.GeometryUtil.readableArea = function (area, isMetric) {
    if (isMetric === false) {
      var acres = area / 4046.8564224;
      return acres >= 1 ? acres.toFixed(2) + ' acres' : Math.round(area * 10.7639) + ' ft²';
    }
    if (area >= 1e6) return (area / 1e6).toFixed(2) + ' km²';
    if (area >= 1e4) return (area / 1e4).toFixed(2) + ' ha';
    return Math.round(area) + ' m²';
  };

  var map = L.map('map', { zoomControl: false, minZoom: 3, maxZoom: 18 }).setView([28.2, 30.6], 6);
  L.control.zoom({ position: 'bottomright' }).addTo(map);

  var satellite = L.layerGroup([
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
      maxNativeZoom: 17, maxZoom: 18,
      attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics'
    }),
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
      maxNativeZoom: 17, maxZoom: 18, opacity: 0.9
    })
  ]);
  var streets = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 18,
    attribution: '&copy; OpenStreetMap contributors'
  });

  var baseName = 'satellite';
  satellite.addTo(map);

  var drawn = L.featureGroup().addTo(map);     // the area the user drew
  var overlays = L.featureGroup().addTo(map);  // center pin and scanned square
  var scanRect = null;

  function shapeStyle() {
    return baseName === 'satellite'
      ? { color: '#ffffff', weight: 3, opacity: 1, fillColor: '#1b7f83', fillOpacity: 0.25 }
      : { color: '#0f5a5e', weight: 3, opacity: 1, fillColor: '#1b7f83', fillOpacity: 0.2 };
  }

  function rectStyle() {
    return {
      color: baseName === 'satellite' ? '#ffffff' : '#0e2a31',
      weight: 1.5, dashArray: '6 6', fill: false, interactive: false
    };
  }

  function setBase(name) {
    if (name === baseName) return;
    if (name === 'streets') {
      map.removeLayer(satellite);
      streets.addTo(map);
    } else {
      map.removeLayer(streets);
      satellite.addTo(map);
    }
    baseName = name;
    els.basemapButtons.forEach(function (btn) {
      btn.setAttribute('aria-pressed', String(btn.dataset.base === name));
    });
    drawn.eachLayer(function (layer) { layer.setStyle(shapeStyle()); });
    if (scanRect) scanRect.setStyle(rectStyle());
  }

  els.basemapButtons.forEach(function (btn) {
    btn.addEventListener('click', function () { setBase(btn.dataset.base); });
  });

  /* ---------------- drawing ---------------- */

  var activeTool = null;
  var activeHandler = null;

  function createHandler(tool) {
    var common = { shapeOptions: shapeStyle(), metric: true };
    if (tool === 'circle') {
      return new L.Draw.Circle(map, Object.assign({ showRadius: true }, common));
    }
    if (tool === 'rectangle') {
      return new L.Draw.Rectangle(map, Object.assign({ showArea: true }, common));
    }
    return new L.Draw.Polygon(map, Object.assign({
      showArea: true,
      allowIntersection: false,
      drawError: { color: '#b02e2e', message: 'Edges cannot cross.' }
    }, common));
  }

  function startTool(tool) {
    if (state.phase === 'loading') return;
    stopTool();
    activeTool = tool;
    activeHandler = createHandler(tool);
    activeHandler.enable();
    render();
  }

  function stopTool() {
    var handler = activeHandler;
    activeHandler = null;
    activeTool = null;
    if (handler) handler.disable();
  }

  els.tools.forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (activeTool === btn.dataset.tool) {
        stopTool();
        render();
      } else {
        startTool(btn.dataset.tool);
      }
    });
  });

  // Fires when a shape is finished and also when drawing is cancelled (Esc).
  map.on(L.Draw.Event.DRAWSTOP, function () {
    activeHandler = null;
    activeTool = null;
    render();
  });

  map.on(L.Draw.Event.CREATED, function (event) {
    setArea(event.layer, event.layerType);
  });

  function pinIcon() {
    return L.divIcon({
      className: 'center-pin',
      html: '<span></span><i class="ping"></i>',
      iconSize: [28, 28],
      iconAnchor: [14, 14]
    });
  }

  function setArea(layer, kind) {
    resetScan();
    drawn.clearLayers();
    overlays.clearLayers();
    scanRect = null;

    layer.setStyle(shapeStyle());
    drawn.addLayer(layer);

    var info;
    if (kind === 'circle') {
      var c = layer.getLatLng();
      info = Geo.circleInfo({ lat: c.lat, lng: c.lng }, layer.getRadius());
    } else {
      var ring = layer.getLatLngs()[0].map(function (p) { return { lat: p.lat, lng: p.lng }; });
      info = Geo.polygonInfo(ring);
    }

    var buffer = Geo.bufferFor(info.radiusM);
    state.area = {
      kind: kind,
      center: info.center,
      radiusM: info.radiusM,
      areaM2: info.areaM2,
      outside: info.outside,
      bufferM: buffer.bufferM,
      capped: buffer.capped,
      tiny: buffer.tiny
    };

    L.marker([info.center.lat, info.center.lng], {
      icon: pinIcon(), interactive: false, keyboard: false
    }).addTo(overlays);

    scanRect = L.rectangle(
      Geo.scanBounds(info.center.lat, info.center.lng, buffer.bufferM),
      rectStyle()
    ).addTo(overlays);

    state.phase = 'ready';
    render();
  }

  function resetAdvice() {
    var adv = state.advice;
    adv.requestId += 1;
    if (adv.controller) {
      adv.controller.abort();
      adv.controller = null;
    }
    adv.phase = 'idle';
    adv.data = null;
    adv.error = null;
  }

  function reveal(el, block) {
    var calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    el.scrollIntoView({ behavior: calm ? 'auto' : 'smooth', block: block || 'nearest' });
  }

  function resetScan() {
    cancelRequest();
    resetAdvice();
    state.hasScanned = false;
    state.result = null;
    state.error = null;
  }

  function cancelRequest() {
    state.requestId += 1;
    if (state.controller) {
      state.controller.abort();
      state.controller = null;
    }
  }

  function clearArea() {
    stopTool();
    resetScan();
    drawn.clearLayers();
    overlays.clearLayers();
    scanRect = null;
    state.area = null;
    state.phase = 'idle';
    render();
  }

  els.clearBtn.addEventListener('click', clearArea);

  /* ---------------- scanning ---------------- */

  function runScan() {
    var area = state.area;
    if (!area || state.phase === 'loading') return;

    stopTool();
    state.requestId += 1;
    var id = state.requestId;
    var controller = new AbortController();
    state.controller = controller;
    resetAdvice();
    state.phase = 'loading';
    state.error = null;
    render();
    reveal(els.reading);

    var timer = setTimeout(function () { controller.abort('timeout'); }, SCAN_TIMEOUT_MS);

    fetch(API.scan, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lat: area.center.lat,
        lon: area.center.lng,
        buffer_m: area.bufferM,
        days: Number(els.daysSelect.value)
      }),
      signal: controller.signal
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        return { res: res, data: data };
      });
    }).then(function (reply) {
      if (id !== state.requestId) return;
      if (reply.res.ok && reply.data && reply.data.ok) {
        state.result = reply.data;
        state.phase = 'done';
      } else {
        var err = reply.data && reply.data.error;
        if (err && err.details) console.error('Scan failed:', err.details);
        state.error = {
          message: err && err.message
            ? err.message
            : 'The server answered with an error (' + reply.res.status + '). Try again.'
        };
        state.phase = 'error';
      }
    }).catch(function () {
      if (id !== state.requestId) return;
      var timedOut = controller.signal.aborted && controller.signal.reason === 'timeout';
      state.error = {
        message: timedOut
          ? 'The scan took too long and was stopped. Press Refresh to try again.'
          : 'Cannot reach the SaltScope server. Check that python server.py is running, then reload this page.'
      };
      state.phase = 'error';
    }).then(function () {
      clearTimeout(timer);
      if (id !== state.requestId) return;
      state.controller = null;
      state.hasScanned = true;
      render();
    });
  }

  els.startBtn.addEventListener('click', runScan);
  els.refreshBtn.addEventListener('click', runScan);

  els.daysSelect.addEventListener('change', function () {
    // A different imagery window makes the shown result out of date.
    if (state.area && (state.phase === 'done' || state.phase === 'error')) {
      resetAdvice();
      state.result = null;
      state.error = null;
      state.phase = 'ready';
      render();
    }
  });

  /* ---------------- AI soil advice ---------------- */

  function runAdvice() {
    var res = state.result;
    var adv = state.advice;
    if (!res || state.phase !== 'done' || adv.phase === 'loading') return;

    adv.requestId += 1;
    var id = adv.requestId;
    var controller = new AbortController();
    adv.controller = controller;
    adv.phase = 'loading';
    adv.error = null;
    adv.data = null;
    render();
    reveal(els.advice);

    var timer = setTimeout(function () { controller.abort('timeout'); }, ADVICE_TIMEOUT_MS);

    fetch(API.advice, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lat: res.location.lat,
        lon: res.location.lon,
        days: res.location.days,
        buffer_m: res.location.buffer_m,
        score: res.result.score,
        bare_soil_fraction: res.bare_soil_fraction,
        mean_ndvi: res.mean_ndvi,
        indices: res.indices
      }),
      signal: controller.signal
    }).then(function (reply) {
      return reply.json().catch(function () { return null; }).then(function (data) {
        return { res: reply, data: data };
      });
    }).then(function (reply) {
      if (id !== adv.requestId) return;
      if (reply.res.ok && reply.data && reply.data.ok) {
        adv.data = reply.data;
        adv.phase = 'done';
      } else {
        var err = reply.data && reply.data.error;
        if (err && err.details) console.error('Advice failed:', err.details);
        adv.error = err && err.message
          ? err.message
          : 'The server answered with an error (' + reply.res.status + '). Try again.';
        adv.phase = 'error';
      }
    }).catch(function () {
      if (id !== adv.requestId) return;
      var timedOut = controller.signal.aborted && controller.signal.reason === 'timeout';
      adv.error = timedOut
        ? 'The AI took too long to answer. Gemini may be busy. Try again in a minute.'
        : 'Cannot reach the SaltScope server. Check that python server.py is running.';
      adv.phase = 'error';
    }).then(function () {
      clearTimeout(timer);
      if (id !== adv.requestId) return;
      adv.controller = null;
      render();
      if (adv.phase === 'done') reveal(els.advice, 'start');
    });
  }

  els.adviceBtn.addEventListener('click', runAdvice);

  /* ---------------- copy coordinates ---------------- */

  var copyTimer = null;

  function flashCopy(text) {
    els.copyLabel.textContent = text;
    clearTimeout(copyTimer);
    copyTimer = setTimeout(function () { els.copyLabel.textContent = 'Copy coordinates'; }, 1800);
  }

  function fallbackCopy(text) {
    var box = document.createElement('textarea');
    box.value = text;
    box.setAttribute('readonly', '');
    box.style.position = 'fixed';
    box.style.opacity = '0';
    document.body.appendChild(box);
    box.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(box);
    return ok;
  }

  els.copyBtn.addEventListener('click', function () {
    if (!state.area) return;
    var text = Geo.formatCoord(state.area.center.lat) + ', ' + Geo.formatCoord(state.area.center.lng);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { flashCopy('Copied'); },
        function () { flashCopy(fallbackCopy(text) ? 'Copied' : 'Copy failed'); }
      );
    } else {
      flashCopy(fallbackCopy(text) ? 'Copied' : 'Copy failed');
    }
  });

  /* ---------------- server check ---------------- */

  function checkServer() {
    fetch(API.health, { cache: 'no-store' })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        state.server = { reachable: true, credentials: !!data.credentials, ai: !!data.ai };
        render();
      })
      .catch(function () {
        state.server = { reachable: false, credentials: true, ai: true };
        render();
      });
  }

  /* ---------------- rendering ---------------- */

  function setStep(el, value) {
    el.dataset.state = value;
  }

  function cell(tag, text) {
    var node = document.createElement(tag);
    node.textContent = text;
    return node;
  }

  function fmt(value) {
    return Number(value).toFixed(4);
  }

  function render() {
    var phase = state.phase;
    var area = state.area;
    var loading = phase === 'loading';
    var blocked = !state.server.reachable || !state.server.credentials;

    document.body.dataset.phase = phase;

    // server notice
    if (!state.server.reachable) {
      els.noticeText.textContent =
        'Cannot reach the SaltScope server. Start it with python server.py and open the address it prints, for example http://127.0.0.1:8000.';
      els.notice.hidden = false;
    } else if (!state.server.credentials) {
      els.noticeText.textContent =
        'The server has no Copernicus credentials, so scans are turned off. Set CDSE_CLIENT_ID and CDSE_CLIENT_SECRET, then restart python server.py.';
      els.notice.hidden = false;
    } else {
      els.notice.hidden = true;
    }

    // steps
    setStep(els.stepArea, area ? 'done' : 'active');
    setStep(els.stepCenter, area ? 'done' : 'todo');
    setStep(els.stepScan, !area ? 'todo' : phase === 'done' ? 'done' : 'active');

    // drawing tools
    els.tools.forEach(function (btn) {
      btn.setAttribute('aria-pressed', String(activeTool === btn.dataset.tool));
      btn.disabled = loading;
    });
    els.clearBtn.hidden = !area;
    els.drawHint.textContent = activeTool ? HINTS[activeTool] : area ? HINTS.done : HINTS.idle;

    // center point
    if (area) {
      els.latValue.textContent = Geo.formatCoord(area.center.lat);
      els.lonValue.textContent = Geo.formatCoord(area.center.lng);
      var kindName = area.kind === 'circle' ? 'Circle' : area.kind === 'rectangle' ? 'Rectangle' : 'Polygon';
      els.areaMeta.textContent = kindName + ', about ' + Geo.formatArea(area.areaM2) + '.';

      var note = '';
      if (area.capped) {
        note = 'This area is large. The scan covers a ' + (Geo.MAX_BUFFER_M * 2 / 1000) +
          ' km square around the center (the dashed outline).';
      } else if (area.tiny) {
        note = 'This area is very small. The scan covers at least a ' + (Geo.MIN_BUFFER_M * 2) +
          ' m square around the center (the dashed outline).';
      } else if (area.outside) {
        note = 'The center falls outside your shape. The scan still uses this point.';
      }
      els.areaNote.textContent = note;
      els.areaNote.hidden = !note;
    } else {
      els.latValue.textContent = '–';
      els.lonValue.textContent = '–';
      els.areaMeta.textContent = 'The center appears here once you draw an area.';
      els.areaNote.hidden = true;
    }
    els.copyBtn.disabled = !area;

    // scan controls
    els.daysSelect.disabled = loading;
    els.startBtn.disabled = !area || loading || blocked;
    els.startLabel.textContent = loading ? 'Scanning…' : 'Start salinity scan';
    els.refreshBtn.disabled = !area || loading || blocked || !state.hasScanned;

    renderStatus();
    renderAdvice();
  }

  function renderAdvice() {
    var adv = state.advice;
    var show = state.phase === 'done';
    els.advice.hidden = !show;
    if (!show) return;

    var loading = adv.phase === 'loading';
    var noKey = state.server.reachable && !state.server.ai;

    els.adviceBtn.disabled = loading || noKey || !state.server.reachable;
    els.adviceBtnLabel.textContent = loading
      ? 'Asking AI…'
      : adv.phase === 'idle' ? 'Get soil advice' : 'Ask again';

    var msg = '';
    var kind = '';
    if (noKey) {
      msg = 'AI advice is turned off. Set GEMINI_API_KEY and restart python server.py.';
      kind = 'info';
    } else if (loading) {
      msg = 'Loading. Asking AI about this soil. This usually takes a few seconds, but can take a minute or two if Gemini is busy and the server has to retry.';
    } else if (adv.phase === 'error') {
      msg = adv.error;
      kind = 'error';
    }
    els.adviceStatus.hidden = !msg;
    els.adviceStatus.dataset.kind = kind;
    els.adviceStatusText.textContent = msg;
    els.adviceSpinner.hidden = !loading;

    var done = adv.phase === 'done' && adv.data;
    els.adviceBody.hidden = !done;
    if (!done) return;

    var advice = adv.data.advice;
    var ctx = adv.data.context;
    els.adviceAssessment.textContent = advice.assessment;

    els.adviceSteps.textContent = '';
    advice.steps.forEach(function (step) {
      var li = document.createElement('li');
      if (step.title) {
        var name = document.createElement('span');
        name.className = 'step-name';
        name.textContent = step.title;
        li.appendChild(name);
      }
      if (step.detail) {
        var detail = document.createElement('span');
        detail.className = 'step-detail';
        detail.textContent = step.detail;
        li.appendChild(detail);
      }
      els.adviceSteps.appendChild(li);
    });

    var hemi = ctx.hemisphere.charAt(0).toUpperCase() + ctx.hemisphere.slice(1) + ' hemisphere';
    var season = /^tropical/.test(ctx.season) ? 'tropical, so wet and dry periods' : ctx.season;
    els.cropsContext.textContent = hemi + ', ' + ctx.month + ' (' + season + '). Matched to the salinity level and likely climate here. Soil type cannot be read from this data.';

    els.adviceCrops.textContent = '';
    advice.crops.forEach(function (crop) {
      var li = document.createElement('li');
      var head = document.createElement('div');
      head.className = 'crop-head';
      var cname = document.createElement('span');
      cname.className = 'crop-name';
      cname.textContent = crop.name;
      head.appendChild(cname);
      if (crop.salt_tolerance) {
        var tol = document.createElement('span');
        tol.className = 'crop-tolerance';
        tol.textContent = crop.salt_tolerance + ' salt tolerance';
        head.appendChild(tol);
      }
      li.appendChild(head);
      if (crop.reason) {
        var why = document.createElement('p');
        why.className = 'crop-reason';
        why.textContent = crop.reason;
        li.appendChild(why);
      }
      els.adviceCrops.appendChild(li);
    });

    els.adviceCaution.textContent = advice.caution;
    els.adviceCaution.hidden = !advice.caution;
    els.adviceMeta.textContent = 'Written by AI (Gemini, ' + ctx.model + ').' +
      (ctx.fallback ? ' The main model was busy, so a backup model answered.' : '') +
      ' Confirm with a soil test and a local agronomist before acting.';
  }

  function renderStatus() {
    var phase = state.phase;
    var level = 0;
    var label = '';
    var sub = '';

    if (phase === 'idle') {
      label = 'No area yet';
      sub = 'Draw an area on the map to begin.';
    } else if (phase === 'ready') {
      label = 'Ready to scan';
      sub = 'Press Start salinity scan to check the center of your area.';
    } else if (phase === 'loading') {
      label = 'Loading';
      sub = 'Fetching Sentinel-2 imagery and comparing it with the reference sites. This can take up to a minute.';
    } else if (phase === 'error') {
      label = 'Scan failed';
      sub = state.error ? state.error.message : 'Something went wrong. Press Refresh to try again.';
    } else if (phase === 'done') {
      var r = state.result.result;
      level = r.level;
      label = r.classification;
      sub = r.score.toFixed(1) + '% similarity to the high-salinity reference sites';
    }

    els.reading.dataset.state = phase;
    els.reading.dataset.level = String(level);
    els.statusText.textContent = label;
    els.statusSub.textContent = sub;
    els.spinner.hidden = phase !== 'loading';
    els.dots.hidden = phase !== 'loading';

    var showStrip = phase === 'loading' || phase === 'done';
    els.stripWrap.hidden = !showStrip;
    els.facts.hidden = phase !== 'done';
    els.details.hidden = phase !== 'done';

    if (phase !== 'done') {
      els.statusNote.hidden = true;
      return;
    }

    var res = state.result;
    els.marker.style.left = Geo.clamp(res.result.score, 0, 100) + '%';

    var rel = res.reliability;
    els.relDot.dataset.level = rel.level;
    els.relText.textContent = RELIABILITY[rel.level] + ', ' +
      Math.round(res.bare_soil_fraction * 100) + '% bare soil';

    var side = res.location.buffer_m * 2;
    els.factArea.textContent = side.toLocaleString('en') + ' m × ' + side.toLocaleString('en') + ' m around the center';
    els.factImagery.textContent = 'Last ' + res.location.days + ' days';
    els.factUpdated.textContent = new Date(res.fetched_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

    var noteText = [res.note, rel.level !== 'good' ? rel.text : ''].filter(Boolean).join(' ');
    els.statusNote.textContent = noteText;
    els.statusNote.hidden = !noteText;

    els.indexRows.textContent = '';
    INDEX_NAMES.forEach(function (name) {
      var row = document.createElement('tr');
      row.appendChild(cell('td', name));
      row.appendChild(cell('td', fmt(res.indices[name])));
      row.appendChild(cell('td', fmt(res.reference.low[name])));
      row.appendChild(cell('td', fmt(res.reference.high[name])));
      els.indexRows.appendChild(row);
    });
    els.distLow.textContent = fmt(res.result.distance_low);
    els.distHigh.textContent = fmt(res.result.distance_high);
    els.refCounts.textContent = res.reference.low_count + ' low, ' + res.reference.high_count + ' high';
  }

  render();
  checkServer();
})();

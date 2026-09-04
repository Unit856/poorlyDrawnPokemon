/* Canvas, scope 7.2 - 7.4.
 *
 * Backing store is a fixed 800x800 (scope 7.2) regardless of display size, so
 * the exported PNG is exactly what the scope specifies no matter the window.
 *
 * The background stays transparent: the eraser paints transparency rather than
 * white so Trivia Tricks can frame the drawing cleanly.
 */
(function () {
  "use strict";

  var root = document.getElementById("draw-root");
  if (!root) return;

  var SIZE = parseInt(root.dataset.size, 10) || 800;
  var TOLERANCE = parseInt(root.dataset.tolerance, 10) || 32;
  var UNDO_DEPTH = parseInt(root.dataset.undoDepth, 10) || 30;

  var canvas = document.getElementById("canvas");
  var ctx = canvas.getContext("2d", { willReadFrequently: true });
  canvas.width = SIZE;
  canvas.height = SIZE;

  var state = {
    tool: "brush",
    color: "#1f2430",
    size: 8,
    drawing: false,
    strokes: 0,        // committed strokes; the scope 7.3 emptiness rule
    undo: [],
    redo: [],
    submitting: false
  };

  // --- history ------------------------------------------------------------
  // Snapshots are stored as compressed data URLs rather than ImageData: 30
  // frames of raw 800x800 RGBA would be ~77 MB, while line art compresses to a
  // few hundred KB per frame.

  function snapshot() {
    try {
      return canvas.toDataURL("image/png");
    } catch (e) {
      return null;
    }
  }

  function pushUndo() {
    var snap = snapshot();
    if (!snap) return;
    state.undo.push(snap);
    if (state.undo.length > UNDO_DEPTH) state.undo.shift();
    state.redo.length = 0;
    refreshButtons();
  }

  function restore(dataUrl, done) {
    var img = new Image();
    img.onload = function () {
      ctx.clearRect(0, 0, SIZE, SIZE);
      ctx.drawImage(img, 0, 0);
      if (done) done();
    };
    img.src = dataUrl;
  }

  function undo() {
    if (!state.undo.length) return;
    var current = snapshot();
    var previous = state.undo.pop();
    if (current) state.redo.push(current);
    restore(previous, refreshButtons);
  }

  function redo() {
    if (!state.redo.length) return;
    var current = snapshot();
    var next = state.redo.pop();
    if (current) state.undo.push(current);
    restore(next, refreshButtons);
  }

  function refreshButtons() {
    byId("undo").disabled = state.undo.length === 0;
    byId("redo").disabled = state.redo.length === 0;
  }

  // --- pointer position ---------------------------------------------------

  function pointAt(event) {
    var rect = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) * (SIZE / rect.width),
      y: (event.clientY - rect.top) * (SIZE / rect.height)
    };
  }

  // --- drawing ------------------------------------------------------------

  function beginStroke(event) {
    if (state.tool === "fill") return;
    state.drawing = true;
    pushUndo();

    var p = pointAt(event);
    ctx.save();
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.lineWidth = state.size;
    if (state.tool === "eraser") {
      // Paints transparency, not white, so the background stays clean.
      ctx.globalCompositeOperation = "destination-out";
      ctx.strokeStyle = "rgba(0,0,0,1)";
    } else {
      ctx.globalCompositeOperation = "source-over";
      ctx.strokeStyle = state.color;
    }
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    // A click with no movement should still leave a dot.
    ctx.lineTo(p.x + 0.01, p.y);
    ctx.stroke();
  }

  function extendStroke(event) {
    if (!state.drawing) return;
    var p = pointAt(event);
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
  }

  function endStroke() {
    if (!state.drawing) return;
    state.drawing = false;
    ctx.closePath();
    ctx.restore();
    commitStroke();
  }

  function commitStroke() {
    state.strokes += 1;
    updateStrokeState();
  }

  function updateStrokeState() {
    var empty = state.strokes === 0;
    byId("submit").disabled = empty || state.submitting;
    root.dataset.strokes = String(state.strokes);
  }

  // --- flood fill ---------------------------------------------------------
  // 4-connected, fixed per-channel tolerance, on a rasterised snapshot of the
  // whole canvas (scope 7.2). Leaks through antialiased stroke edges are a
  // known and accepted limitation.

  function floodFill(x, y) {
    x = Math.floor(x);
    y = Math.floor(y);
    if (x < 0 || y < 0 || x >= SIZE || y >= SIZE) return;

    var image = ctx.getImageData(0, 0, SIZE, SIZE);
    var px = image.data;
    var start = (y * SIZE + x) * 4;

    var seed = [px[start], px[start + 1], px[start + 2], px[start + 3]];
    var target = hexToRgba(state.color);

    if (matches(seed, target, 0)) return; // already that colour

    var stack = [start];
    var seen = new Uint8Array(SIZE * SIZE);

    while (stack.length) {
      var offset = stack.pop();
      var pixel = offset / 4;
      if (seen[pixel]) continue;
      seen[pixel] = 1;

      if (!matches([px[offset], px[offset + 1], px[offset + 2], px[offset + 3]], seed, TOLERANCE)) {
        continue;
      }

      px[offset] = target[0];
      px[offset + 1] = target[1];
      px[offset + 2] = target[2];
      px[offset + 3] = target[3];

      var cx = pixel % SIZE;
      var cy = (pixel - cx) / SIZE;
      if (cx > 0) stack.push(offset - 4);
      if (cx < SIZE - 1) stack.push(offset + 4);
      if (cy > 0) stack.push(offset - SIZE * 4);
      if (cy < SIZE - 1) stack.push(offset + SIZE * 4);
    }

    ctx.putImageData(image, 0, 0);
  }

  function matches(a, b, tolerance) {
    return (
      Math.abs(a[0] - b[0]) <= tolerance &&
      Math.abs(a[1] - b[1]) <= tolerance &&
      Math.abs(a[2] - b[2]) <= tolerance &&
      Math.abs(a[3] - b[3]) <= tolerance
    );
  }

  function hexToRgba(hex) {
    var n = parseInt(hex.replace("#", ""), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 255];
  }

  // --- events -------------------------------------------------------------

  canvas.addEventListener("pointerdown", function (event) {
    if (event.button !== 0 && event.pointerType === "mouse") return;
    canvas.setPointerCapture(event.pointerId);
    if (state.tool === "fill") {
      pushUndo();
      var p = pointAt(event);
      floodFill(p.x, p.y);
      commitStroke();
      return;
    }
    beginStroke(event);
  });

  canvas.addEventListener("pointermove", extendStroke);
  canvas.addEventListener("pointerup", endStroke);
  canvas.addEventListener("pointercancel", endStroke);
  canvas.addEventListener("pointerleave", endStroke);
  // Stop the browser panning/scrolling while drawing on a tablet.
  canvas.addEventListener("touchstart", function (e) { e.preventDefault(); }, { passive: false });
  canvas.addEventListener("touchmove", function (e) { e.preventDefault(); }, { passive: false });

  function byId(id) { return document.getElementById(id); }

  Array.prototype.forEach.call(document.querySelectorAll("[data-tool]"), function (button) {
    button.addEventListener("click", function () {
      state.tool = button.dataset.tool;
      Array.prototype.forEach.call(document.querySelectorAll("[data-tool]"), function (b) {
        b.classList.toggle("active", b === button);
      });
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll("[data-swatch]"), function (button) {
    button.addEventListener("click", function () {
      state.color = button.dataset.swatch;
      byId("color").value = state.color;
      if (state.tool === "eraser") state.tool = "brush";
      syncToolButtons();
    });
  });

  function syncToolButtons() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-tool]"), function (b) {
      b.classList.toggle("active", b.dataset.tool === state.tool);
    });
  }

  byId("color").addEventListener("input", function (e) { state.color = e.target.value; });
  byId("size").addEventListener("input", function (e) {
    state.size = parseInt(e.target.value, 10);
    byId("size-value").textContent = state.size;
  });

  byId("undo").addEventListener("click", undo);
  byId("redo").addEventListener("click", redo);

  byId("clear").addEventListener("click", function () {
    // Scope 7.2: wipes the canvas after a confirm prompt.
    if (!window.confirm("Clear the whole canvas? This cannot be undone with Undo.")) return;
    pushUndo();
    ctx.clearRect(0, 0, SIZE, SIZE);
    state.strokes = 0;
    updateStrokeState();
  });

  document.addEventListener("keydown", function (event) {
    if (!(event.ctrlKey || event.metaKey)) return;
    if (event.key === "z" && !event.shiftKey) { event.preventDefault(); undo(); }
    else if (event.key === "y" || (event.key === "z" && event.shiftKey)) { event.preventDefault(); redo(); }
  });

  // --- submit -------------------------------------------------------------

  function toast(message, kind) {
    var el = byId("toast");
    el.textContent = message;
    el.className = "toast show " + (kind || "error");
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(function () { el.className = "toast"; }, 4000);
  }

  function submit(auto) {
    if (state.submitting) return;
    if (state.strokes === 0) {
      // Scope 7.3: blocked client-side with a toast, no row created.
      toast("Draw something first.");
      return;
    }
    state.submitting = true;
    byId("submit").disabled = true;
    byId("submit").textContent = auto ? "Time up - submitting..." : "Submitting...";

    canvas.toBlob(function (blob) {
      if (!blob) {
        state.submitting = false;
        updateStrokeState();
        toast("Could not read the canvas.");
        return;
      }
      var form = new FormData();
      form.append("image", blob, "drawing.png");
      form.append("strokes", String(state.strokes));

      fetch("/draw/submit", { method: "POST", body: form, credentials: "same-origin" })
        .then(function (response) { return response.json().then(function (b) { return { ok: response.ok, body: b }; }); })
        .then(function (result) {
          if (result.ok && result.body.redirect) {
            window.location.href = result.body.redirect;
            return;
          }
          state.submitting = false;
          byId("submit").textContent = "Submit";
          updateStrokeState();
          toast(result.body.error || "Submission failed.");
        })
        .catch(function () {
          state.submitting = false;
          byId("submit").textContent = "Submit";
          updateStrokeState();
          toast("Network error. Your drawing is still here - try again.");
        });
    }, "image/png");
  }

  byId("submit").addEventListener("click", function () { submit(false); });

  window.addEventListener("beforeunload", function (event) {
    if (state.strokes > 0 && !state.submitting) {
      // The canvas is not persisted across a reload (scope 7.1).
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // --- timer --------------------------------------------------------------

  var timerEl = byId("timer");
  if (timerEl && timerEl.dataset.seconds) {
    var remaining = parseInt(timerEl.dataset.seconds, 10);
    var out = byId("timer-value");

    var tick = window.setInterval(function () {
      remaining -= 1;
      if (remaining <= 10) timerEl.classList.add("low");

      if (remaining <= 0) {
        remaining = 0;
        window.clearInterval(tick);
        timerEl.classList.add("expired");
        render(0);

        if (state.strokes > 0) {
          submit(true);           // scope 7.4: auto-submit the current canvas
        } else {
          // Scope 7.3: an empty canvas on timeout is discarded as a skip.
          fetch("/draw/timeout", { method: "POST", credentials: "same-origin" })
            .then(function (r) { return r.json(); })
            .then(function (b) { window.location.href = b.redirect || "/draw"; })
            .catch(function () { window.location.href = "/draw"; });
        }
        return;
      }
      render(remaining);
    }, 1000);

    function render(seconds) {
      var m = Math.floor(seconds / 60);
      var s = seconds % 60;
      out.textContent = m + ":" + (s < 10 ? "0" : "") + s;
    }
  }

  updateStrokeState();
  refreshButtons();
  syncToolButtons();
})();

/* KiwiEater standalone archive viewer.
 *
 * Loads the portable JSON backup (manifest / blob index / search / gallery)
 * and renders it as a navigable site: each page is shown in an isolated iframe
 * so the original site's own stylesheets and navigation buttons work, while the
 * KiwiEater chrome stays untouched. Internal links are rewritten to in-archive
 * routes; images/media/CSS are rewritten to the on-disk BLOB files; external
 * content is never loaded. Works as plain static files served over HTTP. */

(function () {
  "use strict";

  // Archive root is the parent of this viewer directory.
  var ARCHIVE = new URL("../", location.href).href;
  var TOPURL = location.href.split("#")[0];

  var PLACEHOLDER =
    "data:image/svg+xml;utf8," + encodeURIComponent(
      "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='180'>" +
      "<rect width='100%' height='100%' fill='#11220f'/>" +
      "<text x='50%' y='50%' fill='#3f7f3f' font-family='monospace'" +
      " font-size='14' text-anchor='middle'>image not in archive</text></svg>");

  var manifest = null, pageMap = {}, blobIndex = {}, searchIndex = null,
      gallery = null;

  var $ = function (s) { return document.querySelector(s); };
  var status = function (t) { $("#ke-status").textContent = t; };
  var esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  };

  function getJSON(rel) {
    return fetch(ARCHIVE + rel, { cache: "no-cache" })
      .then(function (r) { if (!r.ok) throw new Error(rel + " " + r.status);
                           return r.json(); });
  }

  function boot() {
    getJSON("manifest.json").then(function (m) {
      manifest = m;
      (m.pages || []).forEach(function (p) { pageMap[p.url] = p; });
      return getJSON("blobs/blob_index.json").catch(function () { return {}; });
    }).then(function (bi) {
      blobIndex = bi || {};
      status("Archive: " + (manifest.counts.pages || 0) + " pages · " +
             (manifest.counts.assets || 0) + " assets · target " +
             manifest.target);
      route();
    }).catch(function (e) {
      status("Could not load manifest.json — has a backup been built yet? (" +
             e.message + ")");
      $("#ke-content").innerHTML =
        "<div class='ke-view'><div class='ke-empty'>No archive manifest found." +
        " Run a backup from the KiwiEater console first.</div></div>";
    });
  }

  // ---- routing ---------------------------------------------------------- //
  function route() {
    var h = location.hash.replace(/^#/, "");
    if (!h || h === "home") return renderPage(manifest.root_url);
    if (h === "index") return renderIndex();
    if (h === "gallery") return renderGallery();
    if (h.indexOf("search") === 0) {
      var q = (h.split("?q=")[1] || "");
      return renderSearch(decodeURIComponent(q));
    }
    if (h.indexOf("u=") === 0) return renderPage(decodeURIComponent(h.slice(2)));
    renderPage(manifest.root_url);
  }
  window.addEventListener("hashchange", route);

  // ---- page rendering (isolated iframe, rewritten references) ----------- //
  function absUrl(ref, base) {
    if (!ref) return "";
    try { return new URL(ref, base || manifest.root_url).href; }
    catch (e) { return ref; }
  }

  // Mirror Python urls.normalize_url so a rewritten reference lands on the same
  // key the crawler stored the BLOB under: drop the fragment, lower-case host,
  // strip default ports + trailing slash, keep the query verbatim.
  function normAbs(url) {
    try {
      var u = new URL(url); u.hash = "";
      var host = u.host.toLowerCase().replace(/:80$/, "").replace(/:443$/, "");
      var path = u.pathname === "/" ? "" : u.pathname.replace(/\/+$/, "");
      return u.protocol + "//" + host + path + (u.search || "");
    } catch (e) { return url; }
  }

  function blobFor(url) {
    if (!url) return null;
    var rec = blobIndex[normAbs(url)] || blobIndex[url];
    return rec ? ARCHIVE + rec.file : null;
  }

  function blobFromSrcset(srcset, base) {
    var parts = String(srcset || "").split(",");
    for (var i = 0; i < parts.length; i++) {
      var u = parts[i].trim().split(/\s+/)[0];
      if (u) { var b = blobFor(absUrl(u, base)); if (b) return b; }
    }
    return null;
  }

  function fetchText(url) {
    return fetch(url, { cache: "no-cache" })
      .then(function (r) { return r.ok ? r.text() : ""; })
      .catch(function () { return ""; });
  }

  // Rewrite every url(...) in a chunk of CSS to its on-disk BLOB (resolved
  // against the stylesheet's own address).  Refs that were never archived fall
  // back to a placeholder so the sandboxed page never reaches out to the live
  // site for a font/sprite/background.
  function rewriteCssUrls(css, baseHref) {
    return String(css || "").replace(
      /url\(\s*(['"]?)([^)'"]+)\1\s*\)/gi, function (m, q, ref) {
        ref = (ref || "").trim();
        if (!ref || /^data:/i.test(ref) || ref.charAt(0) === "#") return m;
        var b = blobFor(absUrl(ref, baseHref));
        return b ? 'url("' + b + '")' : 'url("' + PLACEHOLDER + '")';
      });
  }

  // Fully localise a stylesheet: pull its text, inline every @import
  // recursively, then rewrite url(...) refs to BLOBs — yielding self-contained
  // CSS safe to drop into a <style> inside the sandboxed iframe (relative url()
  // resolution doesn't work against about:srcdoc, so everything must be
  // absolute BLOB URLs).
  function localizeCss(blobUrl, baseHref, seen) {
    seen = seen || {};
    if (seen[blobUrl]) return Promise.resolve("");   // guard @import cycles
    seen[blobUrl] = true;
    return fetchText(blobUrl).then(function (css) {
      var jobs = [];
      var importRe =
        /@import\s+(?:url\(\s*(['"]?)([^)'"]+)\1\s*\)|(['"])([^'"]+)\3)\s*[^;]*;/gi;
      css = css.replace(importRe, function (m, q1, u1, q2, u2) {
        var ref = (u1 || u2 || "").trim();
        var token = "IMP" + jobs.length + "";
        var abs = ref ? absUrl(ref, baseHref) : null;
        var b = abs ? blobFor(abs) : null;
        jobs.push(b ? localizeCss(b, abs, seen)
                        .then(function (t) { return [token, t]; })
                    : Promise.resolve([token, ""]));
        return token;
      });
      css = rewriteCssUrls(css, baseHref);
      return Promise.all(jobs).then(function (parts) {
        parts.forEach(function (p) { css = css.split(p[0]).join(p[1]); });
        return css;
      });
    });
  }

  // Returns a Promise of the rewritten document HTML.  Stylesheets are turned
  // into self-contained inline <style> blocks (async); everything else is
  // rewritten in place.
  function rewriteDoc(doc, pageUrl) {
    var base = pageUrl || manifest.root_url;

    Array.prototype.forEach.call(doc.querySelectorAll("script,noscript"),
      function (n) { n.remove(); });

    // Inline <style> blocks and style="" attributes carry url() theme refs too.
    Array.prototype.forEach.call(doc.querySelectorAll("style"), function (s) {
      s.textContent = rewriteCssUrls(s.textContent || "", base);
    });
    Array.prototype.forEach.call(doc.querySelectorAll("[style]"), function (el) {
      var v = el.getAttribute("style");
      if (v && v.indexOf("url(") >= 0)
        el.setAttribute("style", rewriteCssUrls(v, base));
    });

    Array.prototype.forEach.call(doc.querySelectorAll("img"), function (img) {
      var b = blobFor(absUrl(img.getAttribute("src"), base));
      if (!b) b = blobFromSrcset(img.getAttribute("srcset"), base);
      img.setAttribute("src", b || PLACEHOLDER);
      img.removeAttribute("srcset");
      img.removeAttribute("loading");   // load every image in the tall iframe
    });

    Array.prototype.forEach.call(
      doc.querySelectorAll("video,audio,source"), function (m) {
        var s = m.getAttribute("src"); if (s) { var b = blobFor(absUrl(s, base));
          if (b) m.setAttribute("src", b); else m.removeAttribute("src"); }
        var ss = m.getAttribute("srcset"); if (ss) {
          var sb = blobFromSrcset(ss, base);
          if (sb) m.setAttribute("srcset", sb); else m.removeAttribute("srcset"); }
        var p = m.getAttribute("poster"); if (p) { var pb = blobFor(absUrl(p, base));
          if (pb) m.setAttribute("poster", pb); }
      });

    Array.prototype.forEach.call(doc.querySelectorAll("a[href]"), function (a) {
      var href = a.getAttribute("href");
      if (!href || href[0] === "#") return;
      var abs;
      try { abs = new URL(href, base).href; } catch (e) { return; }
      var inScope = isInScope(abs);
      if (inScope && pageMap[stripSlash(abs)]) {
        a.setAttribute("href", TOPURL + "#u=" + encodeURIComponent(stripSlash(abs)));
        a.setAttribute("target", "_top");
      } else if (inScope) {
        a.setAttribute("href", TOPURL + "#u=" + encodeURIComponent(stripSlash(abs)));
        a.setAttribute("target", "_top");
        a.setAttribute("title", "not archived");
      } else {
        a.setAttribute("target", "_blank");
        a.setAttribute("rel", "noopener noreferrer");
        a.setAttribute("title", "external link (not archived)");
      }
    });

    // Stylesheets: replace each <link> with a self-contained inlined <style> so
    // the archived theme (and its fonts/sprites/backgrounds) renders offline.
    var links = Array.prototype.slice.call(
      doc.querySelectorAll("link[rel~='stylesheet'][href]"));
    var cssJobs = links.map(function (l) {
      var href = absUrl(l.getAttribute("href"), base);
      var b = blobFor(href);
      if (!b) { if (l.parentNode) l.parentNode.removeChild(l);
                return Promise.resolve(); }
      return localizeCss(b, href, {}).then(function (css) {
        var style = doc.createElement("style");
        style.setAttribute("data-ke-href", href);
        style.textContent = css;
        if (l.parentNode) l.parentNode.replaceChild(style, l);
      });
    });

    return Promise.all(cssJobs).then(function () {
      return "<!DOCTYPE html>" + doc.documentElement.outerHTML;
    });
  }

  function isInScope(url) {
    try {
      var host = new URL(url).hostname.toLowerCase();
      return host === manifest.target || host.endsWith("." + manifest.target);
    } catch (e) { return false; }
  }
  function stripSlash(url) {
    try {
      var u = new URL(url); u.hash = "";
      var path = u.pathname.replace(/\/+$/, "") || "/";
      return u.protocol + "//" + u.host + (path === "/" ? "" : path) + u.search;
    } catch (e) { return url; }
  }

  // Size the iframe to its content, re-fitting as images settle the layout.
  function fitFrame(frame) {
    var win, docel;
    try { win = frame.contentWindow; docel = win.document.documentElement; }
    catch (e) { return; }
    var fit = function () {
      try { frame.style.height = (docel.scrollHeight + 30) + "px"; }
      catch (e) { /* keep min-height */ }
    };
    fit();
    try {
      win.document.querySelectorAll("img").forEach(function (im) {
        if (!im.complete) im.addEventListener("load", fit, { once: true });
      });
    } catch (e) { /* ignore */ }
    [150, 500, 1500].forEach(function (t) { setTimeout(fit, t); });
    if (win.ResizeObserver) {
      try { new win.ResizeObserver(fit).observe(docel); } catch (e) { /* ignore */ }
    }
  }

  function renderPage(url) {
    url = stripSlash(url || manifest.root_url);
    var meta = pageMap[url];
    if (!meta) {
      $("#ke-content").innerHTML =
        "<div class='ke-view'><div class='ke-empty'>This page is not in the " +
        "archive yet:<br><code>" + esc(url) + "</code><br><br>" +
        "<a href='#index'>← back to index</a></div></div>";
      status(url);
      return;
    }
    status("Loading " + url + " …");
    getJSON(meta.file).then(function (rec) {
      var doc = new DOMParser().parseFromString(rec.html || "", "text/html");
      return rewriteDoc(doc, rec.url || url).then(function (html) {
        $("#ke-content").innerHTML =
          "<iframe id='ke-frame' sandbox='allow-same-origin allow-top-navigation " +
          "allow-popups allow-top-navigation-by-user-activation'" +
          " style='width:100%;border:0;display:block;min-height:70vh'></iframe>";
        var frame = $("#ke-frame");
        frame.srcdoc = html;
        frame.onload = function () { fitFrame(frame); };
        status(rec.title || url);
        document.title = (rec.title || "KiwiEater Archive");
        window.scrollTo(0, 0);
      });
    }).catch(function (e) {
      status("Failed to load page JSON: " + e.message);
    });
  }

  // ---- index / gallery / search ---------------------------------------- //
  function renderIndex() {
    var rows = (manifest.pages || []).map(function (p) {
      return "<tr><td>" + p.depth + "</td><td><a href='#u=" +
        encodeURIComponent(p.url) + "'>" + esc(p.title) + "</a></td>" +
        "<td class='u'>" + esc(p.url) + "</td></tr>";
    }).join("");
    $("#ke-content").innerHTML =
      "<div class='ke-view'><h1>Archive Index — " +
      (manifest.pages || []).length + " pages</h1><table><thead><tr>" +
      "<th>depth</th><th>title</th><th>url</th></tr></thead><tbody>" +
      (rows || "<tr><td colspan='3'>No pages archived yet.</td></tr>") +
      "</tbody></table></div>";
    status("Index"); document.title = "KiwiEater · Index";
  }

  function renderGallery() {
    status("Loading gallery…");
    var data = gallery ? Promise.resolve(gallery) : getJSON("gallery.json");
    data.then(function (g) {
      gallery = g;
      var cells = (g.images || []).map(function (i) {
        var src = ARCHIVE + i.file;
        return "<figure><a href='" + src + "' target='_blank'>" +
          "<img loading='lazy' src='" + src + "'></a><figcaption>" +
          Math.round((i.size || 0) / 1024) + " KB</figcaption></figure>";
      }).join("");
      $("#ke-content").innerHTML =
        "<div class='ke-view'><h1>Gallery — " + (g.images || []).length +
        " images</h1><div class='ke-grid'>" +
        (cells || "<p>No images archived yet.</p>") + "</div></div>";
      status("Gallery"); document.title = "KiwiEater · Gallery";
    }).catch(function (e) { status("Gallery unavailable: " + e.message); });
  }

  function renderSearch(q) {
    $("#ke-q").value = q || "";
    var run = searchIndex ? Promise.resolve(searchIndex)
                          : getJSON("search_index.json");
    run.then(function (idx) {
      searchIndex = idx;
      var results = [];
      if (q) {
        var ql = q.toLowerCase();
        results = (idx.entries || []).filter(function (e) {
          return (e.title || "").toLowerCase().indexOf(ql) >= 0 ||
                 (e.excerpt || "").toLowerCase().indexOf(ql) >= 0;
        }).slice(0, 400);
      }
      var rows = results.map(function (r) {
        return "<li><a href='#u=" + encodeURIComponent(r.url) + "'>" +
          esc(r.title) + "</a><div class='snip'>" +
          esc((r.excerpt || "").slice(0, 180)) + "</div></li>";
      }).join("");
      $("#ke-content").innerHTML =
        "<div class='ke-view'><h1>Search</h1><p>" +
        (q ? (results.length + " result(s) for “" + esc(q) + "”")
           : "Enter a character name, thread or keyword above.") +
        "</p><ul class='ke-results'>" + rows + "</ul></div>";
      status("Search"); document.title = "KiwiEater · Search";
    }).catch(function (e) { status("Search index unavailable: " + e.message); });
  }

  // ---- wiring ----------------------------------------------------------- //
  $("#ke-search").addEventListener("submit", function (e) {
    e.preventDefault();
    location.hash = "search?q=" + encodeURIComponent($("#ke-q").value.trim());
  });

  boot();
})();

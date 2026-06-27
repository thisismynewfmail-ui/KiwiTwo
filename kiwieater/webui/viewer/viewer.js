/* KiwiEater standalone archive viewer.
 *
 * Loads the portable JSON backup (manifest / blob index / search / gallery)
 * and renders it as a navigable site: each page is shown in an isolated iframe
 * so the site's own navigation buttons work while the KiwiEater chrome stays
 * untouched. Each archived page is re-themed with the bundled KiwiEater 1950s
 * mainframe stylesheet (archive-theme.css) instead of the live site's own CSS,
 * so the backup is always fully themed rather than skeletal/white. Internal
 * links are rewritten to in-archive routes; images/media are rewritten to the
 * on-disk BLOB files; external content is never loaded. Works as plain static
 * files served over HTTP. */

(function () {
  "use strict";

  // Archive root is the parent of this viewer directory.
  var ARCHIVE = new URL("../", location.href).href;
  var TOPURL = location.href.split("#")[0];
  // Viewer directory — where the bundled archive theme lives next to this file.
  var VIEWER = new URL("./", TOPURL).href;

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

  // The KiwiEater archive theme that travels with every backup.  Fetched once
  // and cached; a compact embedded fallback guarantees a page is never rendered
  // white/unthemed even if archive-theme.css somehow can't be loaded.
  var FALLBACK_THEME =
    "html{background:#0c0a09}body{max-width:1180px;margin:0 auto;" +
    "background:#15110e;color:#e9ddc4;font-family:'Courier New',monospace;" +
    "font-size:15px;line-height:1.6;padding:18px 26px}" +
    "a{color:#7fe0a0}h1,h2,h3,h4,h5,h6{color:#ffb000}" +
    "img{max-width:100%;height:auto}" +
    ".block,.message,.structItem,.node{background:#1b1714;" +
    "border:1px solid #473f37;border-radius:8px;margin:0 0 14px;padding:12px}";
  var themeCss = null;
  function getTheme() {
    if (themeCss != null) return Promise.resolve(themeCss);
    return fetchText(VIEWER + "archive-theme.css").then(function (t) {
      themeCss = (t && t.length) ? t : FALLBACK_THEME;
      return themeCss;
    });
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

  // Rewrite a parsed archived document for offline, in-archive viewing and
  // return its HTML (Promise, for a uniform call site).  The live site's own
  // stylesheets are dropped and the KiwiEater archive theme is injected, so the
  // page always renders in the 1950s console look instead of depending on
  // (often un-captured) site CSS.  Images/media point at on-disk BLOBs and
  // links at in-archive routes.
  function rewriteDoc(doc, pageUrl, theme) {
    var base = pageUrl || manifest.root_url;

    // Strip scripts and the site's own stylesheets — the archive theme replaces
    // them, so captured-or-not site CSS can never leave a page white/skeletal.
    Array.prototype.forEach.call(
      doc.querySelectorAll("script,noscript,link[rel~='stylesheet']"),
      function (n) { n.remove(); });
    // Drop page <style> blocks too (but keep any inside inline <svg> icons) so
    // site rules can't fight the theme; inline style="" is kept (it may carry
    // per-element url() backgrounds, localised below).
    Array.prototype.forEach.call(doc.querySelectorAll("style"), function (s) {
      if (!s.closest || !s.closest("svg")) s.remove();
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

    // Inject the KiwiEater archive theme last so it wins the cascade and the
    // captured page renders in the same 1950s look as the console.
    var head = doc.head || doc.getElementsByTagName("head")[0];
    if (!head) {
      head = doc.createElement("head");
      doc.documentElement.insertBefore(head, doc.documentElement.firstChild);
    }
    var style = doc.createElement("style");
    style.id = "ke-archive-theme";
    style.textContent = theme || FALLBACK_THEME;
    head.appendChild(style);

    return Promise.resolve("<!DOCTYPE html>" + doc.documentElement.outerHTML);
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
    // Load the page body and the archive theme together, then render the page
    // into the iframe with the KiwiEater 1950s theme applied.
    Promise.all([getJSON(meta.file), getTheme()]).then(function (res) {
      var rec = res[0], theme = res[1];
      var doc = new DOMParser().parseFromString(rec.html || "", "text/html");
      return rewriteDoc(doc, rec.url || url, theme).then(function (html) {
        $("#ke-content").innerHTML =
          "<iframe id='ke-frame' sandbox='allow-same-origin allow-top-navigation " +
          "allow-popups allow-top-navigation-by-user-activation'" +
          " style='width:100%;border:0;display:block;min-height:70vh;" +
          "background:#0c0a09'></iframe>";
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

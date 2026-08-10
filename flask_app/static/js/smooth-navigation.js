(function () {
  "use strict";

  const NAV_STATE_KEY = "swt:navigation-state";
  const SCROLL_KEY_PREFIX = "swt:scroll:";
  const RESTORE_ATTEMPTS = 18;
  const RESTORE_INTERVAL_MS = 60;
  const PAGE_TRANSITION_MS = 140;

  if (window.__swtSmoothNavigation && window.__swtSmoothNavigation.installed) {
    return;
  }

  const state = {
    installed: true,
    navigating: false,
    restoreTimer: 0,
  };
  window.__swtSmoothNavigation = state;

  if ("scrollRestoration" in history) {
    history.scrollRestoration = "manual";
  }

  function storageKey(url) {
    const nextUrl = new URL(url, window.location.origin);
    return `${SCROLL_KEY_PREFIX}${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`;
  }

  function saveScroll(url = window.location.href) {
    try {
      sessionStorage.setItem(
        storageKey(url),
        JSON.stringify({
          x: window.scrollX || 0,
          y: window.scrollY || 0,
          at: Date.now(),
        })
      );
    } catch (error) {
      /* Session storage can be unavailable in strict privacy modes. */
    }
  }

  function readScroll(url = window.location.href) {
    try {
      const raw = sessionStorage.getItem(storageKey(url));
      return raw ? JSON.parse(raw) : null;
    } catch (error) {
      return null;
    }
  }

  function setPendingRestore(payload) {
    try {
      sessionStorage.setItem(NAV_STATE_KEY, JSON.stringify(payload));
    } catch (error) {
      /* Ignore storage failures; navigation still works. */
    }
  }

  function takePendingRestore() {
    try {
      const raw = sessionStorage.getItem(NAV_STATE_KEY);
      sessionStorage.removeItem(NAV_STATE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (error) {
      return null;
    }
  }

  function shouldReduceMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function ensureTransitionStyles() {
    if (document.getElementById("swt-navigation-styles")) return;
    const style = document.createElement("style");
    style.id = "swt-navigation-styles";
    style.textContent = `
      html.swt-navigation-active { cursor: progress; }
      body { min-height: 100vh; }
      body.swt-page-loading {
        pointer-events: none;
      }
      @media (prefers-reduced-motion: reduce) {
        html { scroll-behavior: auto !important; }
        body.swt-page-loading { transition: none; }
      }
    `;
    document.head.appendChild(style);
  }

  function markLoading(active) {
    document.documentElement.classList.toggle("swt-navigation-active", active);
    if (document.body) document.body.classList.toggle("swt-page-loading", active);
  }

  function sameOriginNavigableUrl(rawHref) {
    if (!rawHref) return null;
    let url;
    try {
      url = new URL(rawHref, window.location.href);
    } catch (error) {
      return null;
    }
    if (url.origin !== window.location.origin) return null;
    if (!["http:", "https:"].includes(url.protocol)) return null;
    return url;
  }

  function isDownloadOrAsset(url, link) {
    if (link && (link.hasAttribute("download") || link.target && link.target !== "_self")) return true;
    const path = url.pathname.toLowerCase();
    return /\.(?:apk|bin|csv|json|pdf|png|jpe?g|webp|svg|zip|gz|txt|xml|webmanifest)$/i.test(path);
  }

  function shouldInterceptLink(event, link) {
    if (event.defaultPrevented || event.button !== 0) return false;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
    if (!link || link.dataset.noClientRoute !== undefined) return false;
    const url = sameOriginNavigableUrl(link.getAttribute("href"));
    if (!url || isDownloadOrAsset(url, link)) return false;
    return true;
  }

  function findAnchor(hash) {
    if (!hash || hash === "#") return null;
    try {
      return document.getElementById(decodeURIComponent(hash.slice(1))) || document.querySelector(hash);
    } catch (error) {
      return null;
    }
  }

  function scrollToTarget({ hash, scroll, forceTop = false }) {
    window.clearTimeout(state.restoreTimer);
    let attempt = 0;
    const behavior = shouldReduceMotion() ? "auto" : "smooth";

    function run() {
      const anchor = findAnchor(hash);
      if (anchor) {
        anchor.scrollIntoView({ behavior, block: "start" });
        return;
      }
      if (scroll && Number.isFinite(scroll.y)) {
        window.scrollTo({ left: Number(scroll.x) || 0, top: Number(scroll.y) || 0, behavior: attempt ? "auto" : behavior });
        return;
      }
      if (forceTop) {
        window.scrollTo({ left: 0, top: 0, behavior });
      }
      if (!anchor && attempt < RESTORE_ATTEMPTS) {
        attempt += 1;
        state.restoreTimer = window.setTimeout(run, RESTORE_INTERVAL_MS);
      }
    }

    window.requestAnimationFrame(run);
  }

  function restoreInitialScroll() {
    const pending = takePendingRestore();
    const saved = readScroll(window.location.href);
    if (pending && pending.url === window.location.href) {
      scrollToTarget({
        hash: pending.hash,
        scroll: pending.scroll || saved,
        forceTop: pending.forceTop,
      });
      return;
    }
    if (history.state && history.state.swtScroll) {
      scrollToTarget({ scroll: history.state.swtScroll });
      return;
    }
    if (saved) scrollToTarget({ scroll: saved });
  }

  function updateHistoryScroll() {
    const scroll = { x: window.scrollX || 0, y: window.scrollY || 0 };
    const nextState = Object.assign({}, history.state || {}, { swtScroll: scroll });
    history.replaceState(nextState, "", window.location.href);
    saveScroll();
  }

  function renderHtml(html, url, options = {}) {
    const targetUrl = new URL(url, window.location.href);
    const targetHref = targetUrl.href;
    const currentScroll = { x: window.scrollX || 0, y: window.scrollY || 0 };
    const samePath = targetUrl.pathname === window.location.pathname && targetUrl.search === window.location.search;
    const hashOnly = samePath && targetUrl.hash;
    const scroll = hashOnly ? null : readScroll(targetHref) || (options.preserveScroll ? currentScroll : null);

    setPendingRestore({
      url: targetHref,
      hash: targetUrl.hash,
      scroll,
      forceTop: !targetUrl.hash && !scroll && !options.preserveScroll,
    });
    const restorePayload = {
      hash: targetUrl.hash,
      scroll,
      forceTop: !targetUrl.hash && !scroll && !options.preserveScroll,
    };

    if (options.replace) {
      history.replaceState(Object.assign({}, history.state || {}, { swtScroll: currentScroll }), "", targetHref);
    } else if (window.location.href !== targetHref) {
      history.pushState({ swtScroll: currentScroll }, "", targetHref);
    }

    if (typeof window.swtPageTeardown === "function") {
      try {
        window.swtPageTeardown();
      } catch (error) {
        console.warn("Page teardown failed before navigation.", error);
      }
      window.swtPageTeardown = null;
    }

    document.open();
    document.write(html);
    document.close();

    ensureTransitionStyles();
    const restoreAfterRender = () => scrollToTarget(restorePayload);
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", restoreAfterRender, { once: true });
    } else {
      restoreAfterRender();
    }
  }

  async function navigateTo(url, options = {}) {
    const targetUrl = sameOriginNavigableUrl(url);
    if (!targetUrl || state.navigating || isDownloadOrAsset(targetUrl, null)) return false;

    const samePath = targetUrl.pathname === window.location.pathname && targetUrl.search === window.location.search;
    if (samePath && targetUrl.hash) {
      saveScroll();
      history.pushState({ swtScroll: { x: window.scrollX || 0, y: window.scrollY || 0 } }, "", targetUrl.href);
      scrollToTarget({ hash: targetUrl.hash });
      return true;
    }

    state.navigating = true;
    updateHistoryScroll();
    markLoading(true);
    try {
      const response = await fetch(targetUrl.href, {
        credentials: "same-origin",
        cache: "default",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "X-SWT-Client-Route": "1",
        },
      });
      const contentType = response.headers.get("content-type") || "";
      if (!response.ok || !contentType.includes("text/html")) {
        window.location.assign(targetUrl.href);
        return true;
      }
      const html = await response.text();
      renderHtml(html, targetUrl.href, options);
      return true;
    } catch (error) {
      console.warn("Client-side navigation failed; falling back to normal navigation.", error);
      window.location.assign(targetUrl.href);
      return true;
    } finally {
      state.navigating = false;
      markLoading(false);
    }
  }

  window.swtNavigate = navigateTo;
  window.swtRenderNavigatedHtml = function (html, options = {}) {
    renderHtml(html, options.url || window.location.href, Object.assign({ replace: true, preserveScroll: true }, options));
  };

  ensureTransitionStyles();

  document.addEventListener("click", (event) => {
    const link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
    if (!shouldInterceptLink(event, link)) return;
    event.preventDefault();
    navigateTo(link.href, { preserveScroll: false });
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form || form.dataset.noClientRoute !== undefined) return;
    const method = String(form.method || "get").toLowerCase();
    if (method !== "get") return;
    const targetUrl = sameOriginNavigableUrl(form.action || window.location.href);
    if (!targetUrl || isDownloadOrAsset(targetUrl, null)) return;
    event.preventDefault();
    const params = new URLSearchParams(new FormData(form));
    targetUrl.search = params.toString();
    navigateTo(targetUrl.href, { preserveScroll: true });
  });

  window.addEventListener("popstate", () => {
    const scroll = history.state && history.state.swtScroll ? history.state.swtScroll : readScroll(window.location.href);
    navigateTo(window.location.href, { replace: true, preserveScroll: true }).then(() => {
      scrollToTarget({ scroll, hash: window.location.hash });
    });
  });

  window.addEventListener("beforeunload", saveScroll);
  window.addEventListener("pagehide", saveScroll);
  window.addEventListener("scroll", () => {
    window.clearTimeout(state.scrollSaveTimer);
    state.scrollSaveTimer = window.setTimeout(updateHistoryScroll, 120);
  }, { passive: true });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restoreInitialScroll, { once: true });
  } else {
    restoreInitialScroll();
  }

})();

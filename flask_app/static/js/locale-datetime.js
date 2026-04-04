(function (global) {
    "use strict";

    const BACKEND_TIMESTAMP_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$/;
    const BACKEND_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
    const HOUR_BUCKET_RE = /^(\d{1,2})(?::(\d{2}))?$/;

    function uniqueValues(values) {
        return Array.from(new Set(values.filter(Boolean)));
    }

    function preferredLocales() {
        const navLocales = Array.isArray(global.navigator && global.navigator.languages)
            ? global.navigator.languages
            : [];
        const fallbackLanguage = global.navigator && global.navigator.language ? [global.navigator.language] : [];
        const htmlLang = global.document && global.document.documentElement
            ? [global.document.documentElement.getAttribute("lang")]
            : [];
        return uniqueValues(
            navLocales
                .concat(fallbackLanguage)
                .concat(htmlLang)
                .concat(["en"])
                .map((value) => String(value || "").trim())
        );
    }

    const localeList = preferredLocales();

    function resolvedOptions(sampleOptions) {
        try {
            return new Intl.DateTimeFormat(localeList, sampleOptions).resolvedOptions();
        } catch (error) {
            try {
                return new Intl.DateTimeFormat(undefined, sampleOptions).resolvedOptions();
            } catch (innerError) {
                return { locale: localeList[0] || "en", timeZone: "" };
            }
        }
    }

    const runtimeLocale = resolvedOptions({ dateStyle: "medium", timeStyle: "short" });

    if (global.document && global.document.documentElement) {
        if (runtimeLocale.locale) {
            global.document.documentElement.setAttribute("lang", runtimeLocale.locale);
            global.document.documentElement.dataset.userLocale = runtimeLocale.locale;
        }
        if (runtimeLocale.timeZone) {
            global.document.documentElement.dataset.userTimeZone = runtimeLocale.timeZone;
        }
    }

    function formatWithLocale(value, options) {
        return new Intl.DateTimeFormat(localeList, options).format(value);
    }

    function parseBackendTimestamp(value) {
        const raw = String(value ?? "").trim();
        const match = raw.match(BACKEND_TIMESTAMP_RE);
        if (!match) {
            return null;
        }
        const [, year, month, day, hour = "0", minute = "0", second = "0"] = match;
        if (match[4] === undefined) {
            return new Date(Number(year), Number(month) - 1, Number(day));
        }
        return new Date(Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second)));
    }

    function formatDateTime(value, fallback = "--", options = { dateStyle: "medium", timeStyle: "short" }) {
        const raw = String(value ?? "").trim();
        if (!raw) {
            return fallback;
        }
        const parsed = parseBackendTimestamp(raw);
        if (!parsed || Number.isNaN(parsed.getTime())) {
            return raw || fallback;
        }
        const safeOptions = BACKEND_DATE_RE.test(raw) ? { dateStyle: options.dateStyle || "medium" } : options;
        try {
            return formatWithLocale(parsed, safeOptions);
        } catch (error) {
            return raw || fallback;
        }
    }

    function formatDate(value, fallback = "--", options = { dateStyle: "medium" }) {
        return formatDateTime(value, fallback, options);
    }

    function formatHourBucket(value) {
        const raw = String(value ?? "").trim();
        const match = raw.match(HOUR_BUCKET_RE);
        if (!match) {
            return raw;
        }
        const sample = new Date();
        sample.setHours(Number(match[1]), Number(match[2] || 0), 0, 0);
        try {
            return formatWithLocale(sample, { hour: "numeric", minute: match[2] ? "2-digit" : undefined });
        } catch (error) {
            return raw;
        }
    }

    function compactLabel(value) {
        const raw = String(value ?? "").trim();
        if (!raw) {
            return "";
        }
        if (BACKEND_DATE_RE.test(raw)) {
            return formatDate(raw, raw, { month: "short", day: "numeric" });
        }
        if (BACKEND_TIMESTAMP_RE.test(raw)) {
            return formatDateTime(raw, raw, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
        }
        if (HOUR_BUCKET_RE.test(raw)) {
            return formatHourBucket(raw);
        }
        return raw;
    }

    function describe() {
        return {
            locale: runtimeLocale.locale || localeList[0] || "en",
            timeZone: runtimeLocale.timeZone || "",
        };
    }

    function applyUtcDateTimeNodes(scope) {
        const root = scope && typeof scope.querySelectorAll === "function" ? scope : global.document;
        if (!root || typeof root.querySelectorAll !== "function") {
            return;
        }
        root.querySelectorAll("[data-utc-datetime]").forEach((node) => {
            const raw = node.getAttribute("data-utc-datetime");
            node.textContent = formatDateTime(raw, node.textContent || "--");
            if (raw) {
                node.title = raw + " UTC";
            }
        });
    }

    global.swtLocale = {
        compactLabel,
        describe,
        formatDate,
        formatDateTime,
        formatHourBucket,
        localeList: localeList.slice(),
        parseBackendTimestamp,
        applyUtcDateTimeNodes,
    };
})(window);

(function (global) {
    "use strict";

    const BACKEND_TIMESTAMP_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$/;
    const BACKEND_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
    const HOUR_BUCKET_RE = /^(\d{1,2})(?::(\d{2}))?$/;
    const DISPLAY_TIME_ZONE = "Asia/Kolkata";
    const DISPLAY_TIME_ZONE_LABEL = "IST";

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
                .concat(["en-IN", "en"])
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

    const runtimeLocale = resolvedOptions({ dateStyle: "medium", timeStyle: "short", timeZone: DISPLAY_TIME_ZONE });

    if (global.document && global.document.documentElement) {
        if (runtimeLocale.locale) {
            global.document.documentElement.setAttribute("lang", runtimeLocale.locale);
            global.document.documentElement.dataset.userLocale = runtimeLocale.locale;
        }
        if (runtimeLocale.timeZone) {
            global.document.documentElement.dataset.userTimeZone = DISPLAY_TIME_ZONE;
        }
    }

    function formatWithLocale(value, options) {
        return new Intl.DateTimeFormat(localeList, { ...options, timeZone: DISPLAY_TIME_ZONE }).format(value);
    }

    function formatConsoleTimestamp() {
        const parts = new Intl.DateTimeFormat("en-CA", {
            timeZone: DISPLAY_TIME_ZONE,
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
            hour12: false,
        }).formatToParts(new Date()).reduce((acc, part) => {
            acc[part.type] = part.value;
            return acc;
        }, {});
        return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} ${DISPLAY_TIME_ZONE_LABEL}`;
    }

    function installConsoleTimestamps() {
        const consoleObject = global.console;
        if (!consoleObject || consoleObject.__swtIstTimestamped) {
            return;
        }
        ["debug", "error", "info", "log", "trace", "warn"].forEach((method) => {
            const original = consoleObject[method];
            if (typeof original !== "function") {
                return;
            }
            consoleObject[method] = function (...args) {
                original.call(consoleObject, `[${formatConsoleTimestamp()}]`, ...args);
            };
        });
        Object.defineProperty(consoleObject, "__swtIstTimestamped", { value: true });
    }

    function parseBackendTimestamp(value) {
        const raw = String(value ?? "").trim();
        const match = raw.match(BACKEND_TIMESTAMP_RE);
        if (!match) {
            return null;
        }
        const [, year, month, day, hour = "0", minute = "0", second = "0"] = match;
        if (match[4] === undefined) {
            return new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
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

    function formatDateTimeWithZoneLabel(value, fallback = "--", options = { dateStyle: "medium", timeStyle: "short" }) {
        const formatted = formatDateTime(value, fallback, options);
        const raw = String(value ?? "").trim();
        if (!formatted || formatted === fallback || BACKEND_DATE_RE.test(raw)) {
            return formatted;
        }
        return /\bIST\b/.test(formatted) ? formatted : `${formatted} ${DISPLAY_TIME_ZONE_LABEL}`;
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
            node.textContent = formatDateTimeWithZoneLabel(raw, node.textContent || "--");
            if (raw) {
                node.title = raw + " UTC, shown in IST";
            }
        });
    }

    installConsoleTimestamps();

    global.swtLocale = {
        compactLabel,
        describe,
        formatDate,
        formatDateTime,
        formatDateTimeWithZoneLabel,
        formatHourBucket,
        localeList: localeList.slice(),
        parseBackendTimestamp,
        applyUtcDateTimeNodes,
    };
})(window);

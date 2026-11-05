const REFRACTION_FILTER_ID = "keyforge-liquid-refraction";

/**
 * O backdrop-filter com url(#svg) não é suportado de forma consistente em
 * WebKit/Safari. Nesses casos mantemos o blur() + border + box-shadow.
 * A classe fica desligada automaticamente quando o filtro não existe ou o
 * navegador não passou no feature check.
 */
export function initLiquidGlass() {
  if (typeof window === "undefined" || typeof document === "undefined") return false;

  const root = document.documentElement;
  try {
    if (!document.getElementById(REFRACTION_FILTER_ID)) return false;

    const supportsFilteredBackdrop =
      window.CSS?.supports?.("backdrop-filter", `url("#${REFRACTION_FILTER_ID}")`) ||
      window.CSS?.supports?.("-webkit-backdrop-filter", `url("#${REFRACTION_FILTER_ID}")`);

    if (!supportsFilteredBackdrop) return false;

    const ua = navigator.userAgent || "";
    const isSafari = /Safari/i.test(ua) && !/Chrome|Chromium|CriOS|Edg|OPR|Firefox|FxiOS/i.test(ua);
    const isIOS = /iP(hone|ad|od)/i.test(ua);

    if (isSafari || isIOS) return false;

    root.dataset.liquidGlass = "on";
    return true;
  } catch {
    return false;
  }
}

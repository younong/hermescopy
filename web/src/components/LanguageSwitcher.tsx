import { useState, useRef, useEffect } from "react";
import { createPortal } from "react-dom";
import { Check, Languages } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { BottomSheet } from "@nous-research/ui/ui/components/bottom-sheet";
import { Typography } from "@nous-research/ui/ui/components/typography/index";
import { useBelowBreakpoint } from "@nous-research/ui/hooks/use-below-breakpoint";
import { LOCALE_META, useI18n } from "@/i18n";
import type { Locale } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * Language picker — shows the current language's endonym, opens a dropdown
 * of all supported locales when clicked. Pass `allowedLocales` to constrain the
 * options for a product surface. Persists choice to localStorage via the I18n
 * context.
 *
 * Replaces the older two-state EN↔ZH toggle now that we ship 16 locales
 * (en, zh, zh-hant, ja, de, es, fr, tr, uk, af, ko, it, ga, pt, ru, hu).
 *
 * No country flags by design — languages aren't countries, and flag pairings
 * inevitably create political mismappings (e.g. Mandarin variants ≠ any single
 * jurisdiction, English ≠ GB, Portuguese ≠ PT). Endonyms are unambiguous.
 *
 * When placed at the bottom of the sidebar (next to ThemeSwitcher), pass
 * `dropUp` so the list opens above the trigger and avoids clipping below the
 * viewport / overflow ancestors. Below the `sm` breakpoint, `dropUp` uses a
 * bottom sheet portaled to `document.body` instead of an anchored dropdown.
 */
export function LanguageSwitcher({
  allowedLocales,
  collapsed = false,
  dropUp = false,
  variant = "default",
}: LanguageSwitcherProps) {
  const { locale, setLocale, t } = useI18n();
  const [open, setOpen] = useState(false);
  const [dropUpPosition, setDropUpPosition] = useState<{ bottom: number; left: number }>();
  const containerRef = useRef<HTMLDivElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const initializedRestrictionRef = useRef(false);
  const narrowViewport = useBelowBreakpoint(640);
  const useMobileSheet = Boolean(variant !== "chat" && dropUp && narrowViewport);
  const allLocales = localeOptions(allowedLocales);
  const normalizedLocale = allLocales.some(([code]) => code === locale)
    ? locale
    : allLocales[0]?.[0] ?? locale;

  useEffect(() => {
    const shouldPersistInitialRestriction =
      allowedLocales !== undefined && !initializedRestrictionRef.current;
    initializedRestrictionRef.current = true;
    if (shouldPersistInitialRestriction || normalizedLocale !== locale) {
      setLocale(normalizedLocale);
    }
  }, [allowedLocales, locale, normalizedLocale, setLocale]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  useEffect(() => {
    if (!open || useMobileSheet) return;
    if (dropUp) {
      const rect = containerRef.current?.getBoundingClientRect();
      if (rect) {
        setDropUpPosition({
          bottom: window.innerHeight - rect.top + 4,
          left: rect.left,
        });
      }
    }

    function onPointerDown(e: PointerEvent) {
      const target = e.target as Node;
      if (containerRef.current?.contains(target)) return;
      if (dropdownRef.current?.contains(target)) return;
      setOpen(false);
    }

    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [dropUp, open, useMobileSheet]);

  const current = LOCALE_META[normalizedLocale];
  const sheetTitle = t.language.switchTo;

  return (
    <div
      ref={containerRef}
      className={cn("relative inline-flex", variant === "chat" && "flex w-full")}
    >
      <Button
        ghost
        onClick={() => setOpen((v) => !v)}
        title={t.language.switchTo}
        aria-label={t.language.switchTo}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={cn(
          "px-2 py-1 normal-case tracking-normal font-normal text-xs text-text-secondary hover:text-foreground",
          collapsed && "hover:bg-transparent",
          variant === "chat" && "gui-chat-language-trigger",
        )}
      >
        <span className="inline-flex min-w-0 items-center gap-2">
          {variant === "chat" ? (
            <Languages aria-hidden className="h-[18px] w-[18px] shrink-0" />
          ) : null}
          <Typography
            className={cn(
              "text-display tracking-wide text-xs",
              variant !== "chat" && "hidden sm:inline",
            )}
          >
            {normalizedLocale === "en" ? "EN" : current.name}
          </Typography>
        </span>
      </Button>

      {useMobileSheet && (
        <BottomSheet
          backdropDismissLabel={t.common.close}
          onClose={() => setOpen(false)}
          open={open}
          title={sheetTitle}
        >
          <div aria-label={sheetTitle} role="listbox">
            <LanguageSwitcherOptions
              allLocales={allLocales}
              locale={normalizedLocale}
              setLocale={setLocale}
              setOpen={setOpen}
            />
          </div>
        </BottomSheet>
      )}

      {open && !useMobileSheet && (() => {
        const dropdown = (
          <div
            ref={dropdownRef}
            aria-label={sheetTitle}
            className={cn(
              "min-w-[10rem] border border-border bg-popover shadow-md py-1 max-h-80 overflow-y-auto",
              dropUp ? "fixed z-[100]" : "absolute z-50 right-0 top-full mt-1",
              variant === "chat" && "gui-chat-language-menu font-sans",
            )}
            role="listbox"
            style={dropUp ? dropUpPosition : undefined}
          >
            <LanguageSwitcherOptions
              allLocales={allLocales}
              locale={normalizedLocale}
              setLocale={setLocale}
              setOpen={setOpen}
            />
          </div>
        );
        return dropUp ? createPortal(dropdown, document.body) : dropdown;
      })()}
    </div>
  );
}

function LanguageSwitcherOptions({
  allLocales,
  locale,
  setLocale,
  setOpen,
}: LanguageSwitcherOptionsProps) {
  return (
    <>
      {allLocales.map(([code, meta]) => {
        const selected = code === locale;

        return (
          <button
            aria-selected={selected}
            className={cn(
              "w-full text-left px-3 py-1.5 flex items-center gap-2 cursor-pointer",
              "font-sans text-display text-xs tracking-[0.08em]",
              "hover:bg-accent hover:text-accent-foreground transition-colors",
              selected ? "font-semibold text-foreground" : "text-muted-foreground",
            )}
            key={code}
            onClick={() => {
              setLocale(code);
              setOpen(false);
            }}
            role="option"
            type="button"
          >
            <span className="truncate">{meta.name}</span>

            {selected && <Check className="ml-auto h-3 w-3 shrink-0 text-midground" />}
          </button>
        );
      })}
    </>
  );
}

interface LanguageSwitcherOptionsProps {
  allLocales: Array<[Locale, (typeof LOCALE_META)[Locale]]>;
  locale: Locale;
  setLocale: (code: Locale) => void;
  setOpen: (open: boolean) => void;
}

function localeOptions(
  allowedLocales?: readonly Locale[],
): Array<[Locale, (typeof LOCALE_META)[Locale]]> {
  if (!allowedLocales) {
    return Object.entries(LOCALE_META) as Array<
      [Locale, (typeof LOCALE_META)[Locale]]
    >;
  }

  return Array.from(new Set(allowedLocales))
    .filter((locale) => locale in LOCALE_META)
    .map((locale) => [locale, LOCALE_META[locale]]);
}

interface LanguageSwitcherProps {
  allowedLocales?: readonly Locale[];
  collapsed?: boolean;
  dropUp?: boolean;
  variant?: "chat" | "default";
}

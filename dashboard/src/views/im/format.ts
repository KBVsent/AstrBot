const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

function clock(date: Date, locale: string): string {
  return new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}

/** Session list time: clock today, "yesterday", weekday within a week, then a date. */
export function formatListTime(iso: string, locale: string, yesterday: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  const days = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS);
  if (days <= 0) return clock(date, locale);
  if (days === 1) return yesterday;
  if (days < 7) return new Intl.DateTimeFormat(locale, { weekday: 'short' }).format(date);
  if (date.getFullYear() === now.getFullYear()) {
    return new Intl.DateTimeFormat(locale, { month: 'numeric', day: 'numeric' }).format(date);
  }
  return new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'numeric', day: 'numeric' }).format(date);
}

/** Time separator in the message stream. */
export function formatSeparator(iso: string, locale: string, yesterday: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const now = new Date();
  const days = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS);
  if (days <= 0) return clock(date, locale);
  if (days === 1) return `${yesterday} ${clock(date, locale)}`;
  const options: Intl.DateTimeFormatOptions = { month: 'numeric', day: 'numeric' };
  if (date.getFullYear() !== now.getFullYear()) options.year = 'numeric';
  return `${new Intl.DateTimeFormat(locale, options).format(date)} ${clock(date, locale)}`;
}

export function formatFullTime(iso: string, locale: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'medium' }).format(date);
}

/** Label for a component name from the segment.other table; own keys only. */
export function markerLabel(labels: unknown, name: string): string | undefined {
  if (!labels || typeof labels !== 'object' || !Object.prototype.hasOwnProperty.call(labels, name)) {
    return undefined;
  }
  const label = (labels as Record<string, unknown>)[name];
  return typeof label === 'string' ? label : undefined;
}

/** Localize the backend's [Name] markers; a name without a label may be user text and stays. */
export function localizeMarkers(text: string, labels: unknown): string {
  return text.replace(/\[([A-Za-z_]+)\]/g, (marker, name: string) => markerLabel(labels, name) ?? marker);
}

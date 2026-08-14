import { en } from "./en";
import type { Translations } from "./types";

export function kanbanTranslations(
  translations: Translations,
): Required<Translations["kanban"]> {
  return { ...en.kanban, ...translations.kanban } as Required<Translations["kanban"]>;
}

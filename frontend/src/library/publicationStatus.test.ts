import { expect, it } from "vitest";
import { publicationStatus } from "./publicationStatus";

it.each([
  ["0", "Finalizado"], ["completed", "Finalizado"],
  ["1", "En publicación"], ["ongoing", "En publicación"],
  ["2", "En pausa"], ["hiatus", "En pausa"],
  ["3", "Cancelado"], ["cancelled", "Cancelado"],
  ["Sin confirmar", "Sin confirmar"],
])("presenta el estado %s como %s", (value, expected) => {
  expect(publicationStatus(value)).toBe(expected);
});

const labels: Record<string, string> = {
  "0": "Finalizado", completed: "Finalizado",
  "1": "En publicación", ongoing: "En publicación",
  "2": "En pausa", hiatus: "En pausa",
  "3": "Cancelado", cancelled: "Cancelado",
};

export function publicationStatus(value: string): string {
  return labels[value.trim().toLowerCase()] ?? value;
}

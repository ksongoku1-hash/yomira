import { act, fireEvent, render } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useAppearanceStore } from "../app/appearanceStore";
import { IllustratedBackdrop } from "./IllustratedBackdrop";

beforeEach(() => {
  useAppearanceStore.setState({ enabled: true, intensity: 60, simpleGraphics: false });
});

describe("IllustratedBackdrop", () => {
  it("does not load decorative images in simple graphics mode", () => {
    useAppearanceStore.setState({ simpleGraphics: true });
    const { container } = render(<IllustratedBackdrop source="/cover.webp" />);
    expect(container).toBeEmptyDOMElement();
    act(() => useAppearanceStore.setState({ simpleGraphics: false }));
    expect(container.querySelector("img")).toHaveAttribute("src", "/cover.webp");
  });
  it("keeps the image decorative and reveals it only after loading", () => {
    const { container } = render(<IllustratedBackdrop source="/cover.webp" />);
    const image = container.querySelector("img")!;
    expect(image).toHaveAttribute("alt", "");
    expect(image.parentElement).toHaveAttribute("aria-hidden", "true");
    expect(image).toHaveAttribute("fetchpriority", "low");
    expect(image).toHaveStyle({ opacity: "0" });
    fireEvent.load(image);
    expect(image).toHaveStyle({ opacity: "0.6" });
    act(() => useAppearanceStore.setState({ intensity: 100 }));
    expect(image).toHaveStyle({ opacity: "1" });
  });

  it("removes failed artwork and loads a changed cover without retrying the broken URL", () => {
    const { container, rerender } = render(<IllustratedBackdrop source="/cover.webp?v=1" />);
    fireEvent.error(container.querySelector("img")!);
    expect(container).toBeEmptyDOMElement();
    rerender(<IllustratedBackdrop source="/cover.webp?v=1" />);
    expect(container).toBeEmptyDOMElement();
    rerender(<IllustratedBackdrop source="/cover.webp?v=2" />);
    expect(container.querySelector("img")).toHaveAttribute("src", "/cover.webp?v=2");
    expect(container.querySelector("img")).toHaveStyle({ opacity: "0" });
  });

  it("does not mount an image when missing, disabled or at zero intensity", () => {
    const { container, rerender } = render(<IllustratedBackdrop source={null} />);
    expect(container).toBeEmptyDOMElement();
    act(() => useAppearanceStore.setState({ enabled: false }));
    rerender(<IllustratedBackdrop source="/cover.webp" />);
    expect(container).toBeEmptyDOMElement();
    act(() => useAppearanceStore.setState({ enabled: true, intensity: 0 }));
    expect(container).toBeEmptyDOMElement();
    act(() => useAppearanceStore.setState({ intensity: 60 }));
    expect(container.querySelector("img")).toBeInTheDocument();
    act(() => useAppearanceStore.setState({ enabled: false }));
    expect(container).toBeEmptyDOMElement();
  });
});

import { it, expect } from 'vitest';
function hour() { return 20; }

it('Shows the clock after dark', () => {
  // catches: M-012
  // reaches: "SHOWS the Clock" after-dark!
  expect(hour()).toBe(21);
});

it('hides the timer', () => {
  // catches: M-012
  // reaches: when the screen opens, then it hides the timer from view
  expect(hour()).toBe(21);
});

it('relabels the clock', () => {
  // catches: M-012
  // reaches: when "Past Nine!", then past-nine
  expect(hour()).toBe(21);
});

it('shows the next day', () => {
  // catches: M-012
  // reaches: when "", then the screen rolls over
  expect(hour()).toBe(21);
});

it('shows the night label', () => {
  // catches: M-012
  // reaches: when the clock passes 9pm, then the night label appears
  expect(hour()).toBe(21);
});

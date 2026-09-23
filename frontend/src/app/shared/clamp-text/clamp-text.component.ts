import { CommonModule } from '@angular/common';
import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  Input,
  NgZone,
  OnDestroy,
  ViewChild
} from '@angular/core';

// A long free-text value inside a table cell: clamped to a few lines so one
// verbose row can't dwarf every other, with a toggle to read the rest in
// place. The `…` a CSS line-clamp draws is painted by the browser, not an
// element, so it can never be the affordance — this adds a real one, and only
// when the text is actually longer than the clamp allows.
@Component({
  selector: 'app-clamp-text',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './clamp-text.component.html',
  styleUrl: './clamp-text.component.css'
})
export class ClampTextComponent implements OnDestroy {
  @Input() text = '';
  /** Lines to show while collapsed. */
  @Input() lines = 4;

  expanded = false;
  overflowing = false;

  private observer?: ResizeObserver;

  // Whether the text overflows depends on the rendered column width, which
  // changes with the viewport — so it has to be measured, not guessed from the
  // string's length, and re-measured when the column resizes. ResizeObserver
  // fires once on observe, which doubles as the initial measurement.
  @ViewChild('body') set body(ref: ElementRef<HTMLElement> | undefined) {
    this.observer?.disconnect();
    this.observer = undefined;
    if (!ref) return;

    const el = ref.nativeElement;
    // A custom property, not `line-clamp` itself: an inline declaration of the
    // real property would outrank .cell-text.expanded and pin the clamp on.
    el.style.setProperty('--clamp-lines', String(this.lines));

    this.observer = new ResizeObserver(() => {
      // Expanded, the element is unclamped and never overflows — measuring
      // then would clear the flag and take the toggle away with it.
      if (this.expanded) return;
      const overflowing = el.scrollHeight > el.clientHeight + 1;
      if (overflowing === this.overflowing) return;
      // ResizeObserver callbacks are not reliably patched into the Angular
      // zone, so re-enter it rather than leaving the new flag unrendered.
      this.zone.run(() => {
        this.overflowing = overflowing;
        this.cdr.markForCheck();
      });
    });
    this.observer.observe(el);
  }

  constructor(private zone: NgZone, private cdr: ChangeDetectorRef) {}

  ngOnDestroy(): void {
    this.observer?.disconnect();
  }
}

/* Schedule the next read after the current one finishes. Separate instances
   keep sign-in polling from forcing expensive library snapshots. */
const ACTIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 30000;

class AdaptivePoller {
  constructor(refresh, delay) {
    this.refresh = refresh;
    this.delay = delay;
    this.timer = null;
    this.inFlight = null;
    this.forcePending = false;
    this.paused = document.hidden;
    this.failed = false;
  }

  clearTimer() {
    clearTimeout(this.timer);
    this.timer = null;
  }

  schedule() {
    this.clearTimer();
    const delay = this.delay();
    if (this.paused || document.hidden || this.inFlight || delay === null)
      return;
    // An unavailable server should not be hit every two seconds just because
    // the last saved snapshot still shows an active job.
    this.timer = setTimeout(
      () => this.request(),
      this.failed ? Math.max(delay, IDLE_POLL_MS) : delay,
    );
  }

  request(force = false) {
    this.clearTimer();
    if (this.inFlight) {
      // A mutation can finish during an older GET. Coalesce those forced
      // refreshes into one trailing read instead of losing the new job state.
      this.forcePending ||= force;
      return this.inFlight;
    }
    this.inFlight = Promise.resolve()
      .then(async () => {
        do {
          const render = force || this.forcePending;
          force = false;
          this.forcePending = false;
          try {
            this.failed = (await this.refresh(render)) === false;
          } catch {
            // Refresh callbacks display their own errors; always keep recovery
            // scheduled even if a callback unexpectedly rejects.
            this.failed = true;
          }
        } while (this.forcePending && !this.paused && !document.hidden);
      })
      .finally(() => {
        this.inFlight = null;
        this.forcePending = false;
        this.schedule();
      });
    return this.inFlight;
  }

  suspend() {
    this.paused = true;
    this.forcePending = false;
    this.clearTimer();
  }

  resume() {
    // Page-cache restoration can dispatch both pageshow and visibilitychange.
    // Only the transition out of suspension needs an immediate request.
    if (!this.paused) return this.inFlight;
    this.paused = false;
    if (!document.hidden && this.delay() !== null) return this.request(true);
  }
}

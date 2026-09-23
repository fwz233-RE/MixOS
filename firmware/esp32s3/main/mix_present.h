#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_lcd_panel_ops.h"

#ifdef __cplusplus
extern "C" {
#endif

#define MIX_PRESENT_WIDTH 1024
#define MIX_PRESENT_HEIGHT 768
#define MIX_PRESENT_BODY_Y 0
#define MIX_PRESENT_BODY_END 648
#define MIX_PRESENT_TIMEOUT_MS 150
#define MIX_PRESENT_MAX_RECTS 8

typedef struct {
    int x0, y0, x1, y1;
} mix_present_rect_t;

/* One presenter, owned by the main task. Init, present and stats must all be
 * called by that task, on the same core that allocated the RGB driver's ISR.
 * The panel must be a fresh ESP-IDF 5.4.2 ESP32-S3 RGB565 streaming panel, with
 * two driver framebuffers, bounce buffers, no rotation/gap/cache invalidation,
 * and initial current framebuffer 0. No other code may draw/switch/reset it.
 * Deferred presents may leave the idle buffer stale; normal presents restore
 * equality, and fade_begin refuses until that deferred work is synchronized.
 * Register on_frame_buf_complete -> mix_present_frame_complete before init.
 * Init does not own the panel, allocate a canvas, or touch either framebuffer.
 * Failed init can be retried; successful init is one-shot, even after a fault.
 */
esp_err_t mix_present_init(esp_lcd_panel_handle_t raw_panel);

/* Main-task-only input sampling during frame waits. The hook must return in
 * <= 15 ms, never draw/call presenter APIs, and only enqueue input (no UI
 * dispatch). Called at most once per 8 ms, and only with >= 20 ms left in the
 * original frame deadline. NULL disables it. No ISR invokes this callback. */
typedef void (*mix_present_wait_hook_t)(void *ctx);
void mix_present_set_wait_hook(mix_present_wait_hook_t hook, void *ctx);

/* Batch of 1..MIX_PRESENT_MAX_RECTS half-open, nonempty rectangles:
 * 0 <= x0 < x1 <= 1024, similarly y <= 768. rects is a naturally aligned,
 * readable array of count descriptors, separate from both driver buffers.
 * canvas is ALWAYS the uint16_t-aligned base of a complete, separate
 * 1024x768 RGB565 image with a 1024-pixel stride (not packed dirty rows).
 * Keep both inputs alive and unchanged until return. All inputs/rectangles
 * are validated BEFORE any framebuffer write, submit or wait. Overlap is
 * allowed: each rectangle is copied, including repeated overlapping pixels.
 * The first successful call must be ONE full-screen rectangle to establish
 * equality; multiple rectangles covering the screen do not qualify.
 * All rectangles are copied to back, then submitted ONCE with ONE future
 * bounce frame-complete wait, then all are mirrored to the released old front.
 * Success means both driver buffers contain the update; it does not mean the
 * LCD glass has finished refreshing. Failed attempts never mirror. If a prior
 * deferred call left stale regions, those are included/coalesced first; stats
 * count the actual copied union, including any unchanged bounding-box gaps.
 * The frame wait uses a single absolute 150ms deadline; RTOS tick rounding and
 * scheduling may delay the return. Copy time is outside that wait budget.
 * Timeout or draw failure permanently locks all writes until reboot. Later
 * calls return that same error, and init cannot reset the fault. Invalid input
 * and a partial/multi-rectangle first frame return errors without locking or
 * changing framebuffer memory. ANY call cancels an existing fade FIRST,
 * including rejected calls. Main-task/same-core ownership is as for init.
 */
esp_err_t mix_present_rects(const mix_present_rect_t *rects, unsigned count,
                            const uint16_t *canvas);

/* Single-rectangle wrapper with exactly the same validation and contract. */
esp_err_t mix_present_rect(int x0, int y0, int x1, int y1,
                           const uint16_t *canvas);

/* Deferred-mirror batch for continuous scrolling. All validation, the first
 * full-screen requirement, the future-frame wait and fault locking match the
 * ordinary API. Only the scanning buffer is guaranteed current on success;
 * the released buffer may lag in the last batch's regions. No canvas pointer
 * is retained after return. As with ordinary dirty updates, canvas is a full
 * image and rects must cover ALL changes since the preceding successful call.
 *
 * Each later ordinary/deferred call automatically includes the stale regions
 * before submitting back. Repeated identical body updates need ONE copy, not
 * copy+immediate mirror. Pending/new regions may be coalesced when doing so
 * reduces writes; if the combined descriptor count exceeds the bound, their
 * bounding boxes may include unchanged gaps from the complete canvas. An
 * ordinary successful present also restores equality of both buffers. A
 * rejected call keeps the pending set; a failed submission locks all writes.
 * fade_begin rejects a pending set; use a normal successful present first.
 * This is copy deferral only: submission and acknowledgement are synchronous.
 */
esp_err_t mix_present_rects_deferred(const mix_present_rect_t *rects,
                                     unsigned count, const uint16_t *canvas);

/* Single-image fade, with the same main-task/same-core ownership as present.
 * Begin supports two initial scenes: (1) both driver bodies already equal
 * canvas, for fading OUT; or (2) both driver bodies equal background and the
 * final target has been rendered into canvas without presenting, for fading
 * IN. An outgoing step(0), followed by end, already establishes (2): rendering
 * a new target into canvas needs no extra background/full-screen submission.
 * First ever present must still cover the FULL screen. In either case bottom
 * navigation/status [648,768) must already be correct and identical to canvas.
 * The caller guarantees these pixel-content preconditions; begin only checks
 * init/first-full/alias/alignment/state.
 * canvas is a complete, separate, uint16_t-aligned 1024x768 RGB565 image. Keep
 * it alive and unchanged from begin until end, normal present, or fault. Begin
 * only scans [0,648) once, builds per-row min/max spans, and never reads or
 * writes a driver buffer, submits, waits, or allocates. Scratch is < 8 KiB.
 * A second begin while active returns INVALID_STATE, retaining the old fade;
 * other rejected begins also leave it unchanged. No rejected call latches a fault.
 *
 * step maps EACH original RGB565 channel c toward background channel b:
 * floor((c * opacity + b * (255-opacity) + 127) / 255). Thus 0 is exactly the
 * background, 255 is exactly canvas, and increasing/decreasing/repeated opacity
 * works without accumulated rounding. Only nonempty per-row spans are written
 * (including background gaps inside each span). Bottom navigation/status and
 * all pixels outside spans remain untouched. One back-buffer update, one
 * submit/future-frame wait, then memcpy of those results to the old front;
 * no second-image alpha blend.
 * The absolute 150ms wait deadline/fault lock are identical to normal present.
 * Empty-body step succeeds without writes/submit/wait/stats. Step without an
 * active fade returns INVALID_STATE. A fault drops the reference and later
 * begin/step/present return the latched error, even after end.
 *
 * end is idempotent, also before init/after fault: drop references only; never
 * change scanout, submit a final frame, or unlock. step(255) does NOT auto-end.
 * ANY normal mix_present_rect/rects call cancels first, even if rejected.
 */
esp_err_t mix_present_fade_begin(const uint16_t *canvas, uint16_t background);
esp_err_t mix_present_fade_step(uint8_t opacity);
void mix_present_fade_end(void);

/* ISR only: call exactly once per actual on_frame_buf_complete, NOT VSYNC or
 * on_color_trans_done; do not queue/replay callbacks. Safe before/during init
 * and after a fault. Return this value from the main RGB callback (or OR it
 * with other ISR wake results) so the driver can yield to a higher-priority
 * task. There is no logging, blocking wait, or framebuffer write in this ISR.
 */
bool mix_present_frame_complete(void);

/* Fixed-width main-task snapshot. Durations are microseconds; last_us/max_us
 * cover copy + submit + wait + mirror, wait_us is the last attempt's wait.
 * Counts/durations/bytes include only valid, started attempts. present_count
 * counts SUCCESSFUL updates (one per batch, not per rectangle); failure_count
 * counts latched submit/wait faults. bytes_copied counts actual CPU writes:
 * ordinary batches without pending work count each rectangle twice on success,
 * once on failure, including overlap. Deferred batches copy once and carry
 * stale regions to the next call; deferred/pending batches coalesce rectangles
 * as described above. Fade span fills/copies and mirrors are also counted.
 * pixels_blended counts lookup-mapped pixels written to back only (opacity
 * 1..254), including background gaps; endpoints and mirrors are copies/fills,
 * not blends.
 * Rejected calls do not overwrite the last attempt's stats.
 * frame_count counts callbacks while ready, including idle/early callbacks;
 * it is not a count of completed presents. initialized/locked are 0 or 1.
 */
typedef struct {
    uint64_t bytes_copied;
    uint64_t pixels_blended;
    uint64_t total_us;
    uint64_t total_wait_us;
    uint32_t present_count;
    uint32_t failure_count;
    uint32_t last_us;
    uint32_t max_us;
    uint32_t wait_us;
    uint32_t frame_count;
    uint32_t initialized;
    uint32_t locked;
    int32_t last_error;
} mix_present_stats_t;

/* Also available before init (all zeros). NULL returns ESP_ERR_INVALID_ARG. */
esp_err_t mix_present_get_stats(mix_present_stats_t *out);

#ifdef __cplusplus
}
#endif

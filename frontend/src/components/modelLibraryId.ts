/**
 * The DOM id of the model library region.
 *
 * Its own module because two components need it and neither should have to
 * import the other for a string: the header's toggle points at it with
 * `aria-controls`, and `ModelLibrary` is what carries it. Keeping it here also
 * keeps `Header.tsx` and `ModelLibrary.tsx` as component-only modules, which
 * is the same reason `installProgress.ts` is separate from
 * `InstallProgressBar.tsx`.
 */

/** `id` of the `<section>` the header's models button shows and hides. */
export const MODEL_LIBRARY_ID = 'model-library'

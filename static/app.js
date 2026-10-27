// Kept out of the HTML so the Content-Security-Policy can forbid inline
// script, which is most of what stops an injected <script> from running.
document.querySelectorAll('input[type=range][data-output]').forEach(function (slider) {
  var out = document.getElementById(slider.dataset.output);
  if (!out) return;
  var show = function () { out.textContent = slider.value + '% price'; };
  slider.addEventListener('input', show);
  show();
});

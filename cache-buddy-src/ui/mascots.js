/* Cache Buddy mascots — smooth inline-SVG characters, one per family.
   All motion is CSS keyframes (60fps) plus a small JS scheduler that
   gives the eyeless brand marks their occasional 360° spins.

   Effects never cover the face: the only props are sparkles (sent) in
   the corners and z's (none) above-right of the art.

   Public API (unchanged for app.js):
     Mascots.init(bigEl, nubEl)
     Mascots.create(el, {family, mode, side, dueLeft}) -> instance
     Mascots.setMode / setFamily / setSide / setDue / colorOf
   Design-sheet helpers:
     Mascots.art(family, expr) -> svg markup string
     Mascots.swatches(family) -> [[hex, label], ...]
     Mascots.exprs / Mascots.families / Mascots.eyed(fam)
*/
"use strict";
const Mascots = (() => {

  let UID = 0;

  const FAMS = ["claude", "devin", "devinmark", "gpt", "gemini", "kimi",
    "glm", "deepseek"];
  const EYED = { claude: 1, devin: 1 };    // kimi has eyes but blinks only

  const STATE_COLOR = {
    guarding: "#3FA46A", busy: "#E0A042", injecting: "#3FA46A",
    sent: "#3FA46A", retry: "#D14343", failed: "#D14343",
    none: "#9CA3AF", loading: "#9CA3AF",
  };

  // mode -> expression class on the wrapper (.e-* picks the eye variant)
  const EXPR = {
    none: "closed", loading: "open", guarding: "open", busy: "right",
    injecting: "open", sent: "happy", retry: "dizzy", failed: "dizzy",
  };

  // ---------------------------------------------------------------- art
  // Every builder returns inner SVG for a 96x96 viewBox. The character's
  // larger dimension is ~52 units, centered on 48,48 (~62% of where the
  // old ring's inner diameter was; now ~just the mascot box size).

  // Claude Code's Clawd.tsx proportions: an 8u x 5u figure (u = 8 svg
  // units => 64 x 40), centered in the 96 box.
  //   body 6u x 4u, arms 1u x 1u at mid rows outside the body,
  //   eyes 0.5u x 1u, four 0.5u x 1u legs as two pairs with a wide
  //   middle gap, bottom 0.5u + legs in the darker shade.
  function claudeBody() {
    const eyes = `<g class="eyes" fill="#2B211C">
      <g class="eyes-open">
        <rect x="32" y="36" width="4" height="8" rx="1"/>
        <rect x="60" y="36" width="4" height="8" rx="1"/>
      </g>
      <g class="eyes-closed">
        <rect x="32" y="39" width="4" height="2" rx="1"/>
        <rect x="60" y="39" width="4" height="2" rx="1"/>
      </g>
      <g class="eyes-left" transform="translate(-2,0)">
        <rect x="32" y="36" width="4" height="8" rx="1"/>
        <rect x="60" y="36" width="4" height="8" rx="1"/>
      </g>
      <g class="eyes-right" transform="translate(2,0)">
        <rect x="32" y="36" width="4" height="8" rx="1"/>
        <rect x="60" y="36" width="4" height="8" rx="1"/>
      </g>
      <g class="eyes-happy" fill="none" stroke="#2B211C" stroke-width="1.8"
          stroke-linecap="round">
        <path d="M32,42 Q34,36.5 36,42"/>
        <path d="M60,42 Q62,36.5 64,42"/>
      </g>
      <g class="eyes-dizzy" fill="none" stroke="#2B211C" stroke-width="1.8"
          stroke-linecap="round">
        <path d="M32,37 L36,43 M36,37 L32,43"/>
        <path d="M60,37 L64,43 M64,37 L60,43"/>
      </g>
    </g>`;
    return `<g class="body">
      <rect class="arm" x="16" y="44" width="8" height="8" rx="1.4" fill="#D97757"/>
      <rect class="arm" x="72" y="44" width="8" height="8" rx="1.4" fill="#D97757"/>
      <rect x="24" y="28" width="48" height="32" rx="1.6" fill="#D97757"/>
      <path d="M24,56 L72,56 L72,58.4 A1.6,1.6 0 0 1 70.4,60 L25.6,60
               A1.6,1.6 0 0 1 24,58.4 Z" fill="#B85C3E"/>
      <rect class="leg" x="28" y="60" width="4" height="8" rx="1" fill="#B85C3E"/>
      <rect class="leg" x="36" y="60" width="4" height="8" rx="1" fill="#B85C3E"/>
      <rect class="leg" x="56" y="60" width="4" height="8" rx="1" fill="#B85C3E"/>
      <rect class="leg" x="64" y="60" width="4" height="8" rx="1" fill="#B85C3E"/>
      ${eyes}
    </g>`;
  }

  // Devin's real logo path (from their app bundle, "devin-logo",
  // 48x48 source scaled to ~52 units tall). Chain-link trefoil.
  const DEVIN_PATH =
    "M18.5847 20.1289L18.5847 20.1321C19.2629 19.7407 20.0334 19.5339 20.8136 19.5339L20.8709 19.5339L20.9521 19.5355C21.0062 19.537 21.0587 19.5386 21.1129 19.5434L21.1527 19.5466C21.7959 19.5943 22.4072 19.7757 22.9772 20.0892C23.0345 20.121 23.0902 20.1528 23.1459 20.1862C23.1889 20.2133 23.2319 20.2403 23.2765 20.269L23.3179 20.296C23.6697 20.5331 23.9865 20.8211 24.2667 21.1584C24.3049 21.2045 24.3415 21.2507 24.3861 21.3095L24.4084 21.3382C24.4371 21.3764 24.4657 21.4161 24.4928 21.4559L24.5421 21.5275C24.5644 21.5609 24.5851 21.5943 24.6058 21.6278C24.6233 21.6564 24.6408 21.685 24.6584 21.7137L24.6886 21.7646C25.0802 22.4408 25.2872 23.2141 25.2872 23.9969L25.2856 23.9969C25.2856 24.7813 25.0787 25.553 24.687 26.2292L24.6186 26.3454C24.5899 26.3915 24.5612 26.4361 24.5326 26.4806L24.5103 26.5141C24.1473 27.0471 23.6856 27.4846 23.1268 27.8219C23.0711 27.8554 23.0154 27.8872 22.958 27.919C22.9135 27.9445 22.8673 27.9667 22.8211 27.9906L22.7766 28.0129C22.3945 28.199 21.9869 28.3295 21.5554 28.4027C21.4965 28.4122 21.4376 28.4202 21.3787 28.4282L21.3294 28.4345C21.2816 28.4393 21.2339 28.4441 21.1845 28.4488C21.1558 28.4504 21.1272 28.4536 21.0985 28.4552C21.0587 28.4584 21.0205 28.46 20.9807 28.46C20.9473 28.46 20.9139 28.4616 20.8804 28.4632L20.8199 28.4632L20.8167 28.4632C20.035 28.4632 19.2645 28.2563 18.5863 27.8649L14.4533 25.4814L6.18421 30.2611L6.18102 39.7998L14.4501 44.5715L22.7224 39.7982L22.7224 35.0265C22.7224 34.2421 22.9294 33.4704 23.321 32.7942L23.3895 32.678C23.4181 32.6319 23.4468 32.5873 23.4755 32.5428L23.4977 32.5093C23.8607 31.9763 24.3224 31.5388 24.8812 31.2015C24.937 31.168 24.9927 31.1362 25.05 31.1044C25.0946 31.0805 25.1392 31.0567 25.1869 31.0328L25.2315 31.0105C25.6136 30.8244 26.0212 30.6939 26.4526 30.6207C26.5115 30.6112 26.5704 30.6032 26.6293 30.5952L26.6787 30.5889C26.7264 30.5841 26.7742 30.5793 26.8251 30.5746L26.9095 30.5682C26.9493 30.565 26.9875 30.5634 27.0273 30.5634C27.0608 30.5634 27.0942 30.5618 27.1276 30.5602L27.1881 30.5602L27.1913 30.5602C27.973 30.5602 28.7436 30.7671 29.4218 31.1585L33.5563 33.542L41.8286 28.7687L41.8254 19.2268L33.5579 14.4535L29.4234 16.8433L29.4202 16.8369C28.7404 17.2283 27.9714 17.4304 27.1849 17.4352L27.1324 17.4352L27.0496 17.4336C26.9955 17.432 26.9429 17.4304 26.8888 17.4256L26.849 17.4225C26.2058 17.3747 25.5945 17.1933 25.0245 16.8799C24.9672 16.8481 24.9115 16.8163 24.8558 16.7828C24.8112 16.7558 24.7682 16.7271 24.7252 16.7001L24.6838 16.6731C24.332 16.436 24.0152 16.148 23.735 15.8107C23.6968 15.7645 23.6601 15.7184 23.6156 15.6595L23.5933 15.6309C23.5646 15.5927 23.536 15.5529 23.5089 15.5131L23.4595 15.4415C23.4373 15.4081 23.4166 15.3747 23.3959 15.3413C23.3783 15.3127 23.3592 15.284 23.3417 15.2538L23.3131 15.2045C22.9214 14.5282 22.7145 13.755 22.7145 12.9721L22.7129 12.9721L22.7129 8.202L14.4836 3.45095L14.4438 3.42867L6.17466 8.20836L6.17147 17.747L14.4406 22.5188L18.5767 20.1321L18.5847 20.1289Z";
  // Factory's logo (Droid's harness), from Factory Desktop's bundle
  // ("factory-logo", viewBox 100 90 700 700). Seven-blade pinwheel.
  const FACTORY_PATH =
    "M582.594 251.356C581.452 251.073 580.385 250.547 579.466 249.813C578.546 249.08 577.795 248.156 577.266 247.106C576.736 246.055 576.44 244.903 576.397 243.728C576.354 242.552 576.565 241.381 577.017 240.295C592.606 202.357 599.485 172.002 588.384 159.294C558.984 125.58 441.083 192.624 403.49 215.331C402.484 215.936 401.358 216.316 400.19 216.446C399.023 216.577 397.841 216.454 396.725 216.086C395.61 215.718 394.587 215.113 393.727 214.314C392.866 213.515 392.188 212.539 391.739 211.453C375.937 173.596 359.325 147.264 342.487 146.122C297.857 143.068 261.885 273.85 251.355 316.475C251.074 317.616 250.55 318.683 249.817 319.603C249.085 320.522 248.162 321.273 247.113 321.803C246.064 322.332 244.912 322.629 243.738 322.672C242.563 322.715 241.393 322.504 240.308 322.052C202.37 306.463 172.002 299.584 159.307 310.685C125.593 340.085 192.624 457.987 215.33 495.579C215.938 496.585 216.32 497.711 216.451 498.879C216.583 500.047 216.46 501.229 216.092 502.346C215.724 503.462 215.119 504.485 214.318 505.346C213.517 506.206 212.54 506.883 211.453 507.33C173.609 523.132 147.277 539.744 146.121 556.581C143.08 601.211 273.85 637.184 316.488 647.714C317.627 647.998 318.691 648.525 319.608 649.259C320.525 649.992 321.273 650.914 321.801 651.963C322.329 653.011 322.624 654.161 322.668 655.335C322.711 656.508 322.501 657.677 322.052 658.761C306.463 696.699 299.584 727.067 310.685 739.762C340.084 773.476 458 706.445 495.592 683.739C496.598 683.131 497.724 682.749 498.892 682.618C500.06 682.486 501.243 682.609 502.359 682.977C503.475 683.345 504.498 683.95 505.358 684.751C506.219 685.552 506.896 686.529 507.344 687.616C523.145 725.46 539.744 751.792 556.594 752.948C601.224 755.989 637.196 625.219 647.713 582.581C647.998 581.441 648.525 580.375 649.259 579.457C649.993 578.539 650.917 577.79 651.967 577.262C653.017 576.733 654.169 576.438 655.344 576.396C656.519 576.354 657.689 576.566 658.775 577.018C696.712 592.607 727.067 599.472 739.775 588.384C773.49 558.985 706.445 441.069 683.738 403.477C683.136 402.47 682.757 401.345 682.628 400.178C682.499 399.011 682.622 397.83 682.99 396.715C683.358 395.6 683.961 394.577 684.76 393.717C685.558 392.856 686.532 392.177 687.616 391.726C725.473 375.924 751.805 359.312 752.947 342.475C756.001 297.845 625.219 261.873 582.594 251.356ZM531.391 208.572C539.969 223.948 495.765 326.408 462.886 398.073C462.337 399.271 461.433 400.273 460.297 400.942C459.161 401.611 457.847 401.917 456.532 401.817C455.217 401.717 453.964 401.217 452.942 400.384C451.92 399.551 451.178 398.424 450.816 397.157C437.537 350.561 422.36 295.813 406.12 249.338C405.482 247.514 405.513 245.522 406.209 243.719C406.905 241.917 408.219 240.42 409.917 239.498C450.471 217.349 519.865 187.936 531.391 208.572ZM337.044 221.253C353.974 226.06 395.165 329.767 422.585 403.69C423.042 404.925 423.111 406.271 422.781 407.546C422.451 408.821 421.739 409.965 420.74 410.824C419.741 411.683 418.503 412.215 417.193 412.35C415.882 412.485 414.562 412.215 413.409 411.577C371.037 388.061 321.627 360.043 277.276 338.664C275.539 337.822 274.157 336.39 273.377 334.625C272.596 332.86 272.468 330.875 273.013 329.023C286.066 284.726 314.297 214.813 337.044 221.253ZM208.585 367.651C223.948 359.073 326.42 403.278 398.073 436.156C399.271 436.706 400.272 437.61 400.942 438.746C401.611 439.882 401.916 441.196 401.816 442.51C401.717 443.825 401.217 445.078 400.383 446.1C399.55 447.122 398.424 447.864 397.156 448.227C350.575 461.506 295.813 476.683 249.337 492.923C247.515 493.557 245.526 493.524 243.727 492.828C241.927 492.133 240.433 490.82 239.511 489.125C217.402 448.572 187.936 379.177 208.585 367.651ZM221.266 561.999C226.06 545.069 329.78 503.878 403.703 476.457C404.938 476 406.284 475.932 407.559 476.262C408.834 476.592 409.978 477.304 410.837 478.303C411.695 479.301 412.228 480.539 412.363 481.85C412.498 483.16 412.228 484.48 411.59 485.633C388.06 528.006 360.042 577.416 338.663 621.754C337.827 623.496 336.398 624.884 334.631 625.668C332.864 626.451 330.876 626.579 329.023 626.029C284.725 613.056 214.813 584.746 221.266 561.999ZM367.664 690.458C359.073 675.094 403.291 572.622 436.169 500.97C436.719 499.772 437.623 498.77 438.759 498.101C439.895 497.431 441.209 497.126 442.523 497.226C443.838 497.326 445.091 497.826 446.113 498.659C447.135 499.492 447.877 500.618 448.24 501.886C461.518 548.468 476.696 603.23 492.936 649.705C493.569 651.529 493.534 653.518 492.836 655.318C492.137 657.118 490.822 658.612 489.125 659.532C448.585 681.641 379.177 711.106 367.704 690.458H367.664ZM562.012 677.777C545.068 672.983 503.877 569.263 476.457 495.34C475.997 494.103 475.927 492.754 476.257 491.477C476.587 490.199 477.301 489.053 478.302 488.193C479.304 487.334 480.545 486.802 481.858 486.669C483.171 486.537 484.493 486.81 485.646 487.452C528.005 510.969 577.429 539 621.767 560.379C623.507 561.217 624.891 562.648 625.672 564.414C626.453 566.181 626.58 568.168 626.029 570.02C612.989 614.384 584.759 684.23 562.012 677.777ZM690.47 531.378C675.094 539.97 572.635 495.751 500.969 462.873C499.771 462.323 498.77 461.42 498.1 460.284C497.431 459.148 497.126 457.834 497.226 456.519C497.325 455.204 497.825 453.952 498.659 452.93C499.492 451.908 500.618 451.165 501.886 450.803C548.481 437.524 603.229 422.346 649.705 406.106C651.531 405.472 653.522 405.508 655.325 406.206C657.127 406.904 658.622 408.219 659.544 409.918C681.64 450.458 711.106 519.866 690.47 531.378ZM677.789 337.03C672.983 353.974 569.275 395.165 495.353 422.586C494.116 423.046 492.767 423.115 491.489 422.785C490.212 422.455 489.066 421.742 488.206 420.74C487.346 419.739 486.815 418.498 486.682 417.185C486.55 415.872 486.823 414.549 487.465 413.396C510.982 371.037 539 321.614 560.379 277.276C561.219 275.537 562.65 274.154 564.416 273.374C566.182 272.593 568.168 272.465 570.019 273.013C614.317 286.053 684.23 314.284 677.789 337.03Z";
  const DEVIN_TF = "translate(17.6,17.6) scale(1.265)";   // 48-src -> ~52 tall

  // Devin's otter (Cognition's mascot is a sea otter; this one is our own
  // drawing, not an official asset): chibi head with a pale face mask,
  // small ears low on the sides, blush, big shiny eyes, hugging a little
  // laptop with the trefoil on its lid, tail curling out behind.
  const OT = { fur: "#8A5A3B", dark: "#5E3B25", ear: "#B47A55",
    cream: "#EED9BD", pale: "#FBF3E8", blush: "#F4A3A0", ink: "#2B211C",
    lid: "#DCE1E8", mark: "#8B93A1" };

  function cuteEyes(lx, rx, cy, r) {
    const one = (x, dx) => `<circle cx="${x + dx}" cy="${cy}" r="${r}"/>
      <circle cx="${x + dx + r * .38}" cy="${cy - r * .4}" r="${r * .36}"
        fill="#fff"/>`;
    const two = dx => one(lx, dx) + one(rx, dx);
    const line = `fill="none" stroke="${OT.ink}" stroke-width="2"
      stroke-linecap="round"`;
    const sleep = x =>
      `<path d="M${x - r},${cy} Q${x},${cy + r * .9} ${x + r},${cy}"/>`;
    const happy = x => `<path d="M${x - r},${cy + r * .4}
      Q${x},${cy - r * 1.2} ${x + r},${cy + r * .4}"/>`;
    const k = r * .75;
    const cross = x => `<path d="M${x - k},${cy - k} L${x + k},${cy + k}
      M${x + k},${cy - k} L${x - k},${cy + k}"/>`;
    return `<g class="eyes" fill="${OT.ink}">
      <g class="eyes-open">${two(0)}</g>
      <g class="eyes-closed" ${line}>${sleep(lx)}${sleep(rx)}</g>
      <g class="eyes-left">${two(-1.6)}</g>
      <g class="eyes-right">${two(1.6)}</g>
      <g class="eyes-happy" ${line}>${happy(lx)}${happy(rx)}</g>
      <g class="eyes-dizzy" ${line}>${cross(lx)}${cross(rx)}</g>
    </g>`;
  }

  function devinBody() {
    const ear = x => `<circle cx="${x}" cy="33" r="4.3" fill="${OT.dark}"/>
      <circle cx="${x}" cy="33" r="1.9" fill="${OT.ear}"/>`;
    const k = .2;                      // trefoil on the lid, 48-src scaled
    return `<g class="body">
      <path d="M62,71 Q79,73 80.5,62 Q81,57 77,58.5 Q75.5,66.5 62,65Z"
        fill="${OT.dark}"/>
      <ellipse cx="38.5" cy="76" rx="6.4" ry="3.6" fill="${OT.dark}"/>
      <ellipse cx="57.5" cy="76" rx="6.4" ry="3.6" fill="${OT.dark}"/>
      <ellipse cx="48" cy="64" rx="18" ry="13" fill="${OT.fur}"/>
      ${ear(24.2)}${ear(71.8)}
      <ellipse cx="48" cy="38" rx="24.5" ry="20" fill="${OT.fur}"/>
      <ellipse cx="48" cy="44" rx="17.5" ry="13" fill="${OT.cream}"/>
      <ellipse cx="43.6" cy="48" rx="5.8" ry="4.6" fill="${OT.pale}"/>
      <ellipse cx="52.4" cy="48" rx="5.8" ry="4.6" fill="${OT.pale}"/>
      <ellipse cx="33" cy="45.5" rx="4.2" ry="2.5" fill="${OT.blush}"
        opacity=".75"/>
      <ellipse cx="63" cy="45.5" rx="4.2" ry="2.5" fill="${OT.blush}"
        opacity=".75"/>
      <path d="M45.2,43.4 Q48,41.9 50.8,43.4 Q49.8,45.9 48,46.2
        Q46.2,45.9 45.2,43.4Z" fill="${OT.ink}"/>
      <path d="M48,46.2 V47.4 M48,47.4 Q46.3,49.2 44.8,48.1
        M48,47.4 Q49.7,49.2 51.2,48.1" fill="none" stroke="${OT.ink}"
        stroke-width="1.15" stroke-linecap="round"/>
      ${cuteEyes(37.6, 58.4, 36, 4.3)}
      <rect x="33.5" y="57" width="29" height="17" rx="3.4" fill="${OT.lid}"/>
      <g class="lid-mark"><path d="${DEVIN_PATH}" fill="${OT.mark}"
        transform="translate(${48 - 24 * k},${65.5 - 24 * k}) scale(${k})"/></g>
      <ellipse cx="35.5" cy="59" rx="4.6" ry="3.8" fill="${OT.fur}"/>
      <ellipse cx="60.5" cy="59" rx="4.6" ry="3.8" fill="${OT.fur}"/>
    </g>`;
  }

  // Devin's geometric trefoil: SWE as one half of a fusion session,
  // where the otter would read as "this whole session is Devin"
  function devinMarkBody() {
    return `<g class="mark"><path d="${DEVIN_PATH}" fill="#3A3A3A"
              transform="${DEVIN_TF}"/></g>`;
  }

  // real OpenAI blossom knot (simple-icons CC0 path, 24x24 source)
  const GPT_PATH =
    "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z";

  function gptBody() {
    return `<g class="mark"><path d="${GPT_PATH}" fill="#1F1F1F"
              transform="translate(22,22) scale(2.167)"/></g>`;
  }

  // real Gemini sparkle silhouette (simple-icons path) over a blurred
  // 4-color blend clipped to the star shape
  const GEMINI_PATH =
    "M11.04 19.32Q12 21.51 12 24q0-2.49.93-4.68.96-2.19 2.58-3.81t3.81-2.55Q21.51 12 24 12q-2.49 0-4.68-.93a12.3 12.3 0 0 1-3.81-2.58 12.3 12.3 0 0 1-2.58-3.81Q12 2.49 12 0q0 2.49-.96 4.68-.93 2.19-2.55 3.81a12.3 12.3 0 0 1-3.81 2.58Q2.49 12 0 12q2.49 0 4.68.96 2.19.93 3.81 2.55t2.55 3.81";

  function geminiBody(uid) {
    const tf = "translate(22,22) scale(2.167)";
    return `<defs>
        <clipPath id="gs${uid}">
          <path d="${GEMINI_PATH}" transform="${tf}"/>
        </clipPath>
        <filter id="gb${uid}" x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="6"/>
        </filter>
      </defs>
      <g class="mark">
        <g clip-path="url(#gs${uid})">
          <rect width="96" height="96" fill="#4285F4"/>
          <g filter="url(#gb${uid})">
            <circle cx="48" cy="26" r="22" fill="#EA4335"/>
            <circle cx="26" cy="48" r="22" fill="#FBBC04"/>
            <circle cx="70" cy="48" r="22" fill="#4285F4"/>
            <circle cx="48" cy="70" r="22" fill="#34A853"/>
          </g>
        </g>
      </g>`;
  }

  // real Z.ai "Z" mark — LobeHub MIT icon set (zai.svg, 24x24 source);
  // the mark itself is monochrome, painted in Zhipu's brand blue #3859FF
  const GLM_PATH =
    "M12.105 2L9.927 4.953H.653L2.83 2h9.276z" +
    "M23.254 19.048L21.078 22h-9.242l2.174-2.952h9.244z" +
    "M24 2L9.264 22H0L14.736 2H24z";

  // DeepSeek's whale mark (24x24 source, the same as LobeHub's MIT set),
  // in DeepSeek's brand blue #4D6BFE
  const DEEPSEEK_PATH = "M23.748 4.651c-.254-.124-.364.113-.512.233-.051.04-.094.09-.137.137-.372.397-.806.657-1.373.626-.829-.046-1.537.214-2.163.848-.133-.782-.575-1.248-1.247-1.548-.352-.155-.708-.311-.955-.65-.172-.24-.219-.509-.305-.774-.055-.16-.11-.323-.293-.35-.2-.031-.278.136-.356.276-.313.572-.434 1.202-.422 1.84.027 1.436.633 2.58 1.838 3.393.137.094.172.187.129.323-.082.28-.18.553-.266.833-.055.179-.137.218-.328.14a5.5 5.5 0 0 1-1.737-1.179c-.857-.828-1.631-1.743-2.597-2.46a12 12 0 0 0-.689-.47c-.985-.957.13-1.743.387-1.836.27-.098.094-.433-.778-.428-.872.003-1.67.295-2.687.685a3 3 0 0 1-.465.136 9.6 9.6 0 0 0-2.883-.101c-1.885.21-3.39 1.1-4.497 2.622C.082 8.776-.231 10.854.152 13.02c.403 2.284 1.568 4.175 3.36 5.653 1.857 1.533 3.997 2.284 6.438 2.14 1.482-.085 3.132-.284 4.994-1.86.47.234.962.328 1.78.398.629.058 1.235-.031 1.705-.129.735-.155.684-.836.418-.961-2.155-1.004-1.682-.595-2.112-.926 1.095-1.295 2.768-3.598 3.284-6.733.05-.346.115-.834.108-1.114-.004-.171.035-.238.23-.257a4.2 4.2 0 0 0 1.545-.475c1.397-.763 1.96-2.016 2.093-3.517.02-.23-.004-.467-.247-.588M11.58 18.168c-2.088-1.642-3.101-2.183-3.52-2.16-.39.024-.32.472-.234.763.09.288.207.487.371.74.114.167.192.416-.113.603-.673.416-1.842-.14-1.897-.168-1.361-.801-2.5-1.86-3.301-3.306-.775-1.393-1.225-2.888-1.299-4.482-.02-.385.094-.522.477-.592a4.7 4.7 0 0 1 1.53-.038c2.131.311 3.946 1.264 5.467 2.774.868.86 1.525 1.887 2.202 2.89.72 1.066 1.494 2.082 2.48 2.915.348.291.626.513.892.677-.802.09-2.14.109-3.055-.615zm1.001-6.44a.306.306 0 0 1 .415-.287.3.3 0 0 1 .113.074.3.3 0 0 1 .086.214c0 .17-.136.307-.308.307a.303.303 0 0 1-.306-.307m3.11 1.596c-.2.081-.4.151-.591.16a1.25 1.25 0 0 1-.798-.254c-.274-.23-.47-.358-.551-.758a1.7 1.7 0 0 1 .015-.588c.07-.327-.007-.537-.238-.727-.188-.156-.426-.199-.689-.199a.6.6 0 0 1-.254-.078.253.253 0 0 1-.114-.358 1 1 0 0 1 .192-.21c.356-.202.767-.136 1.146.016.352.144.618.408 1.001.782.392.451.462.576.685.915.176.264.336.536.446.848.066.194-.02.353-.25.45";

  function deepseekBody() {
    return `<g class="mark"><path d="${DEEPSEEK_PATH}" fill="#4D6BFE"
              transform="translate(20,20) scale(2.333)"/></g>`;
  }

  function glmBody() {
    return `<g class="mark"><path d="${GLM_PATH}" fill="#3859FF"
              transform="translate(22,22) scale(2.167)"/></g>`;
  }

  // solid blue circle, two white pill eyes slightly right of centre in
  // the upper half.  Blink is the only motion (see .f-kimi CSS/tick).
  function kimiBody() {
    return `<g class="body">
      <circle cx="48" cy="48" r="30" fill="#1783FF"/>
      <g class="eyes" fill="#FFFFFF">
        <g class="eyes-open">
          <rect x="45.3" y="32.4" width="7.8" height="14.4" rx="3.9"/>
          <rect x="60.9" y="32.4" width="7.8" height="14.4" rx="3.9"/>
        </g>
        <g class="eyes-closed">
          <rect x="45.3" y="38.4" width="7.8" height="2.4" rx="1.2"/>
          <rect x="60.9" y="38.4" width="7.8" height="2.4" rx="1.2"/>
        </g>
      </g>
    </g>`;
  }

  function artInner(fam, uid) {
    switch (fam) {
      case "claude": return claudeBody();
      case "devin": return devinBody();
      case "devinmark": return devinMarkBody();
      case "gpt": return gptBody();
      case "gemini": return geminiBody(uid);
      case "kimi": return kimiBody();
      case "glm": return glmBody();
      case "deepseek": return deepseekBody();
      default: return "";             // generic/unknown: empty art —
    }                                 // the card slot hides itself
  }

  // ---------------------------------------------------------------- CSS
  const CSS = `
.mascot { position: relative; width: 100%; height: 100%; }
.mascot .inner { position: absolute; inset: 0; }
.mascot.side-left .inner { transform: scaleX(-1); }
.mascot .art {
  position: absolute; inset: 0; width: 100%; height: 100%;
  overflow: visible;
}
.mascot .body, .mascot .mark {
  transform-origin: 48px 48px; transform-box: view-box;
}

/* ------- eyes: one variant visible at a time */
.mascot .eyes > g { display: none; }
.mascot.e-open   .eyes-open   { display: block; }
.mascot.e-closed .eyes-closed { display: block; }
.mascot.e-left   .eyes-left   { display: block; }
.mascot.e-right  .eyes-right  { display: block; }
.mascot.e-happy  .eyes-happy  { display: block; }
.mascot.e-dizzy  .eyes-dizzy  { display: block; }
.mascot.e-open .eyes-open, .mascot.e-right .eyes-right {
  animation: mz-blink-o 4.4s infinite;
}
.mascot.e-open .eyes-closed, .mascot.e-right .eyes-closed {
  display: block; opacity: 0; animation: mz-blink-c 4.4s infinite;
}
@keyframes mz-blink-o { 0%,91%,98%,100% {opacity:1} 93%,96% {opacity:0} }
@keyframes mz-blink-c { 0%,91%,98%,100% {opacity:0} 93%,96% {opacity:1} }
.mascot.m-loading .eyes-open { animation: mz-glance 1.3s steps(1,end) infinite; }
@keyframes mz-glance { 0%,49% {transform:translateX(-3.5px)}
                       50%,100% {transform:translateX(3.5px)} }

/* ------- effects layer: sparkles on sent, z's on none only ------- */
.mascot .fx { position: absolute; inset: 0; pointer-events: none; }
.mascot .fx > * { position: absolute; display: none; }

.mascot .fxz {
  font: 700 11px Consolas, monospace; color: #9CA3AF;
}
.m-none .fxz { display: block; animation: mz-z 3s infinite; opacity: 0; }
.m-none .fxz.z1 { right: 10%; top: 14%; }
.m-none .fxz.z2 { right: 6%;  top: 8%;  animation-delay: 1s; }
.m-none .fxz.z3 { right: 2%;  top: 2%;  animation-delay: 2s; }
@keyframes mz-z {
  0% { transform: translateY(8px) scale(.7); opacity: 0; }
  30% { opacity: .9; }
  100% { transform: translateY(-16px) translateX(4px) scale(1.1); opacity: 0; }
}

.mascot .fx-spark { width: 10%; fill: #F0B33C; }
.m-sent .fx-spark { display: block; animation: mz-spark .9s infinite; }
.mascot .fx-spark.s1 { left: 8%;  top: 8%; }
.mascot .fx-spark.s2 { right: 7%; top: 11%; animation-delay: .3s; }
.mascot .fx-spark.s3 { left: 11%; bottom: 10%; animation-delay: .6s; }
.mascot .fx-spark.s4 { right: 10%; bottom: 8%; animation-delay: .15s; }
@keyframes mz-spark {
  0%,100% { transform: scale(.4) rotate(0deg); opacity: 0; }
  50% { transform: scale(1) rotate(22deg); opacity: 1; }
}

/* ------- eyed families (claude): small bounces ------- */
:is(.f-claude,.f-devin).m-none .art   { animation: mz-breathe 3.6s ease-in-out infinite; }
:is(.f-claude,.f-devin).m-guarding .art { animation: mz-hop 1s steps(2,jump-none) infinite; }
:is(.f-claude,.f-devin).m-injecting .art { animation: mz-jump .55s ease-in-out infinite; }
:is(.f-claude,.f-devin).m-sent .art   { animation: mz-hop2 1.1s ease-in-out infinite; }
@keyframes mz-breathe { 50% { transform: translateY(1.5%) scaleY(1.015); } }
@keyframes mz-hop  { 50% { transform: translateY(-4%); } }
@keyframes mz-hop2 { 0%,100% {transform:translateY(0)} 40% {transform:translateY(-6%)} }
@keyframes mz-jump { 0%,100% {transform:translateY(0) scaleY(1)}
                     45% {transform:translateY(-9%) scaleY(1.04)} }
:is(.f-claude,.f-devin).m-retry .art { animation: mz-wobble .5s ease-in-out infinite; }
:is(.f-claude,.f-devin).m-failed .art { transform: rotate(6deg);
  filter: grayscale(.75) opacity(.75); animation: none; }
@keyframes mz-wobble { 0%,100% {transform:rotate(-3deg)} 50% {transform:rotate(3deg)} }
.m-yawn .art { animation: mz-yawn 1.3s ease-in-out; }
@keyframes mz-yawn {
  0% { transform: translateY(0) scaleY(1); }
  35% { transform: translateY(-4%) scaleY(.92); }
  100% { transform: translateY(4%) scaleY(.94); }
}

/* ------- kimi: no body motion, blink only (.blinking via JS) ------- */
.f-kimi .eyes-open   { display: block; }
.f-kimi.blinking .eyes-open  { display: none; }
.f-kimi.blinking .eyes-closed { display: block; }
.f-kimi.m-none .art  { opacity: .45; }
.f-kimi.m-failed .art { filter: grayscale(.9); }
.f-kimi.e-closed .eyes-open   { display: none; }
.f-kimi.e-closed .eyes-closed { display: block; }

/* ------- eyeless brand marks: life through rotation only ------- */
/* base: slow sway; .spinning swaps in a one-shot 360 for ~1.2s */
:is(.f-devinmark,.f-gpt,.f-gemini,.f-glm,.f-deepseek) .mark {
  animation: mz-sway 6s ease-in-out infinite;
}
:is(.f-devinmark,.f-gpt,.f-gemini,.f-glm,.f-deepseek).spinning .mark {
  animation: mz-rot360 1.2s ease-in-out;
}
@keyframes mz-sway { 0%,100% {transform:rotate(-3deg)}
                     50% {transform:rotate(3deg)} }
@keyframes mz-rot360 { to { transform: rotate(360deg); } }
:is(.f-devinmark,.f-gpt,.f-gemini,.f-glm,.f-deepseek).m-retry .mark {
  animation: mz-wobble .4s ease-in-out infinite;
}
:is(.f-devinmark,.f-gpt,.f-gemini,.f-glm,.f-deepseek).m-none .mark {
  animation: none; opacity: .4;
}
:is(.f-devinmark,.f-gpt,.f-gemini,.f-glm,.f-deepseek).m-failed .mark {
  animation: none; filter: grayscale(.9); opacity: .55;
  transform: rotate(12deg);
}
.f-gpt.m-none .mark path { fill: #D9D3CC; }
.f-gpt.m-failed .mark path { fill: #9CA3AF; }
.f-devinmark.m-failed .mark path { fill: #9CA3AF; }
/* the trefoil on the otter's lid stays readable when the art mirrors */
.mascot.f-devin.side-left .lid-mark {
  transform: scaleX(-1); transform-origin: 48px 65.5px;
  transform-box: view-box;
}
.f-glm.m-failed .mark path { fill: #9CA3AF; }
.f-deepseek.m-failed .mark path { fill: #9CA3AF; }
.f-gemini.m-failed .mark { filter: grayscale(1); }
`;

  // ---------------------------------------------------------------- fx svg
  const SPARK = i => `<svg class="fx-spark s${i}" viewBox="0 0 20 20"><path d="M10,1 Q11.4,8.6 19,10 Q11.4,11.4 10,19 Q8.6,11.4 1,10 Q8.6,8.6 10,1 Z"/></svg>`;
  const FX_HTML =
    '<span class="fxz z1">z</span><span class="fxz z2">z</span>' +
    '<span class="fxz z3">z</span>' +
    SPARK(1) + SPARK(2) + SPARK(3) + SPARK(4);

  // ---------------------------------------------------------------- mascot
  const EYELESS = { devinmark: 1, gpt: 1, gemini: 1, glm: 1, deepseek: 1 };
  const SPIN_EVERY = 30000;             // one 360° spin per 30s per mark

  class Mascot {
    constructor(el, opts) {
      this.el = el;
      this.uid = ++UID;
      this.fam = "claude"; this.mode = "none"; this.side = "right";
      this.dueLeft = null;
      this.spinEvery = (opts && opts.spinEvery) ||
        (typeof window !== "undefined" && window.SPIN_MS) || SPIN_EVERY;
      this._yawnT = null;
      this._nextSpin = 0;
      this._spinT = null;
      this._spinning = false;
      this._blinkAt = Date.now() + 4000;
      this._blinkT = null;
      this._blinking = false;
    }
    render() {
      this.el.innerHTML =
        '<div class="inner">' +
        '<div class="fx">' + FX_HTML + "</div>" +   // behind the art —
        '<svg class="art" viewBox="0 0 96 96">' +   // props emerge from
        artInner(this.fam, this.uid) + "</svg>" +   // behind the mascot
        "</div>";
      this.apply();
    }
    apply() {
      let expr = this.mode === "none" && this._yawnT &&
        Date.now() < this._yawnT ? "open" : (EXPR[this.mode] || "open");
      if (this.fam === "kimi") expr = "open";   // kimi only ever blinks
      const yawning = expr === "open" && this.mode === "none";
      this.el.className =
        "mascot f-" + this.fam + " m-" + this.mode +
        " e-" + expr + " side-" + this.side + (yawning ? " m-yawn" : "") +
        (this._spinning ? " spinning" : "") +
        (this._blinking ? " blinking" : "");
    }
    setMode(m) {
      if (m === this.mode) return;
      const was = this.mode;
      this.mode = m;
      if (was === "guarding" && m === "none") {
        this._yawnT = Date.now() + 1300;
        this.apply();
        setTimeout(() => { if (this.mode === "none") this.apply(); }, 1350);
        return;
      }
      if (m === "sent") this.spin();             // heartbeat went out
      this.apply();
    }
    spin() {
      if (!EYELESS[this.fam]) return;
      this._spinning = true;
      this.apply();
      clearTimeout(this._spinT);
      this._spinT = setTimeout(() => {
        this._spinning = false; this.apply();
      }, 1250);
    }
    tick() {
      const now = Date.now();
      // eyeless marks: one 360 spin per `spinEvery` in every
      // mode except none/failed
      if (EYELESS[this.fam]) {
        if (this.mode !== "none" && this.mode !== "failed") {
          if (!this._nextSpin) this._nextSpin = now + this.spinEvery;
          else if (now >= this._nextSpin) {
            this.spin();
            this._nextSpin = now + this.spinEvery;
          }
        } else this._nextSpin = 0;
      }
      // kimi: ~120ms blink every ~4s, occasional double-blink
      if (this.fam === "kimi" && now >= this._blinkAt) {
        this._blinkAt = now + 4000;
        this._blink();
      }
    }
    _blink() {
      this._blinking = true;
      this.apply();
      clearTimeout(this._blinkT);
      this._blinkT = setTimeout(() => {
        this._blinking = false;
        this.apply();
        if (Math.random() < 0.25)                // occasional double-blink
          setTimeout(() => this._blink(), 240);
      }, 130);
    }
    setFamily(f) {
      if (f === this.fam) return;
      this.fam = FAMS.includes(f) ? f : "";
      this._spinning = false;
      this.render();                      // art changes -> rebuild svg
    }
    setSide(s) { if (s !== this.side) { this.side = s; this.apply(); } }
    setDue(n) { this.dueLeft = n; }
  }

  const list = [];
  setInterval(() => { for (const m of list) m.tick(); }, 120);

  const api = {
    create(el, opts) {
      const m = new Mascot(el, opts);
      if (opts) {
        if (opts.family) m.fam = opts.family;
        if (opts.mode) m.mode = opts.mode;
        if (opts.side) m.side = opts.side;
        if (opts.dueLeft != null) m.dueLeft = opts.dueLeft;
      }
      injectCSS();
      m.render();
      list.push(m);
      return m;
    },
    init(big, nub) {
      list.length = 0;
      if (big) api.create(big);
      if (nub) api.create(nub);
    },
    destroy(m) {                        // track mascots get rebuilt on
      const i = list.indexOf(m);        // signature change — drop the
      if (i >= 0) list.splice(i, 1);    // instance so it stops ticking
      if (m) m.el.innerHTML = "";
    },
    // ---- design-sheet helpers -------------------------------------
    art(fam, expr) {                      // standalone svg, one expression
      injectCSS();
      return '<svg class="art mz e-' + (expr || "open") +
        '" viewBox="0 0 96 96">' + artInner(fam, ++UID) + "</svg>";
    },
    swatches(fam) {
      const map = {
        claude: [["#D97757", "body"], ["#B85C3E", "shade"], ["#2B211C", "eyes"]],
        devin: [["#8A5A3B", "fur"], ["#EED9BD", "face"], ["#F4A3A0", "blush"],
        ["#DCE1E8", "laptop"]],
        devinmark: [["#3A3A3A", "mark"]],
        gpt: [["#1F1F1F", "knot"], ["#D9D3CC", "idle"]],
        gemini: [["#EA4335", "top"], ["#FBBC04", "left"],
        ["#4285F4", "right"], ["#34A853", "bottom"]],
        kimi: [["#1783FF", "body"], ["#FFFFFF", "eyes"]],
        glm: [["#3859FF", "mark"]],
        deepseek: [["#4D6BFE", "mark"]],
      };
      return map[fam] || [];
    },
    // flat brand mark for the harness picker (48x48 source)
    devinLogo: () => '<svg viewBox="0 0 48 48"><path fill="#3A3A3A" d="' +
      DEVIN_PATH + '"/></svg>',
    factoryLogo: () => '<svg viewBox="100 90 700 700"><path fill="#3A3A3A" d="' +
      FACTORY_PATH + '"/></svg>',
    exprs: ["open", "closed", "left", "right", "happy", "dizzy"],
    families: FAMS,
    eyed: fam => !!EYED[fam],
    setMode(m) { for (const x of list) x.setMode(m); },
    setFamily(f) { for (const x of list) x.setFamily(f); },
    setSide(s) { for (const x of list) x.setSide(s); },
    setDue(n) { for (const x of list) x.setDue(n); },
    colorOf: mode => STATE_COLOR[mode] || STATE_COLOR.none,
  };

  let cssDone = false;
  function injectCSS() {
    if (cssDone) return;
    cssDone = true;
    const st = document.createElement("style");
    st.id = "mascot-css";
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  return api;
})();

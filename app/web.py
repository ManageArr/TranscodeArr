"""The web interface, as one self-contained page.

No build step, no framework, no CDN - a NAS container that needs npm to render
its own settings screen is a container that stops rendering its settings screen
the day a registry goes down. Plain HTML and a few hundred lines of vanilla JS,
served from memory.

The credential is held in localStorage and sent as a bearer header - a session
token from the login form and a minted API key are the same thing to this page,
which is why signing in did not need a cookie and this service still has no CSRF
surface. A 401 forgets it and shows the sign-in screen, because a token typed
wrong used to leave a blank page with no way back.
"""

# The project's own mark, in one place because it is wrapped two ways: as the
# favicon document and as the header lockup. Pasted twice it drifts the first
# time somebody nudges a path, and then the tab icon is no longer the logo
# sitting beside the wordmark.
#
# The gradient id is "ta-mark" and not "ta": an inlined SVG shares the page's id
# namespace, and a two-letter id is one some later element claims by accident -
# at which point url(#ta) resolves against that element instead of the gradient
# and the shapes stop painting teal.
_MARK = (
    '<defs><linearGradient id="ta-mark" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#5eead4"/><stop offset="1" stop-color="#14b8a6"/>'
    "</linearGradient></defs>"
    "<!-- Conversion arc: an open loop, so the gap reads as motion rather than a closed ring. -->"
    '<path d="M52 32a20 20 0 1 1-5.9-14.1" fill="none" stroke="url(#ta-mark)" stroke-width="6"'
    ' stroke-linecap="round"/>'
    '<path d="M46.2 5.6v12.8H33.4" fill="none" stroke="url(#ta-mark)" stroke-width="6"'
    ' stroke-linecap="round" stroke-linejoin="round"/>'
    "<!-- Play triangle: the thing being converted is still media. -->"
    '<path d="M26 22.4 42 32 26 41.6z" fill="url(#ta-mark)"/>'
)

# Served at GET /favicon.svg, where the mark is the whole document: the <title>
# is the only thing there that can name it, so this wrapper keeps it.
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="TranscodeArr">'
           "<title>TranscodeArr</title>" + _MARK + "</svg>")

# The header lockup, which is decoration: the wordmark it sits against already
# says the name, so keeping the role, the <title> and the aria-label here would
# have a screen reader announce "TranscodeArr TranscodeArr" on every page.
_INLINE_MARK = '<svg class="mark" viewBox="0 0 64 64" aria-hidden="true">' + _MARK + "</svg>"

PAGE = r"""<!doctype html><meta charset="utf-8"><title>TranscodeArr</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script>/* Runs before the stylesheet and before a single element exists, so the
 first paint is already the chosen theme instead of dark repainted light. */
document.documentElement.dataset.theme=localStorage.theme||'dark'</script>
<style>
 /* VS Code Dark Modern, on the bare :root: dark is what a browser paints with no
    attribute set and no script having run, which is what makes the line above a
    one-liner instead of a theme engine. */
 /* color-scheme is not decoration: it is what tells the browser to paint the
    select's own dropdown list, the number spinners and the scrollbars to match,
    and none of those are ours to style. */
 :root{color-scheme:dark;
       --bg:#1f1f1f;--panel:#181818;--raised:#252526;--line:#2b2b2b;--edge:#3c3c3c;
       --ink:#cccccc;--dim:#9d9d9d;--field:#313131;--hover:#2a2d2e;
       --accent:#0078d4;--accent-hover:#026ec1;--on-accent:#ffffff;--link:#4daafc;
       /* One substitution here too, same reason as the light block below: error
          #f14c4c on the raised surface #252526 is 4.3:1, and the two strings it
          carries there - "will not run on this machine" and a card's Delete -
          are the ones you most need to read. Same hue and saturation, lightness
          62% -> 65%, which is 4.7:1 on raised and 5.4:1 on a card. */
       --ok:#89d185;--bad:#f25a5a;--warn:#cca700;
       /* Derived, because VS Code ships no second step for these: the same hues
          at about two thirds lightness, for the dark end of a meter gradient. */
       --warn-deep:#8b7200;--bad-deep:#a43434}
 /* VS Code Light Modern. Three substitutions, all of them contrast on a white
    page, same hue and darker: success #388a34 reaches about 4.0:1 so it is
    #1a7f37 at about 4.9:1, error #e51400 about 4.3:1 on a raised surface so it
    is #d81400, warning #bf8803 about 3.1:1 so it is #8a6200. --link matches
    --accent here because #005fb8 is already dark enough to read as text. */
 :root[data-theme="light"]{color-scheme:light;--bg:#ffffff;--panel:#f8f8f8;--raised:#f3f3f3;--line:#e5e5e5;--edge:#cecece;
       --ink:#3b3b3b;--dim:#616161;--field:#ffffff;--hover:#f0f0f0;
       --accent:#005fb8;--accent-hover:#0258a8;--on-accent:#ffffff;--link:#005fb8;
       --ok:#1a7f37;--bad:#d81400;--warn:#8a6200;
       --warn-deep:#5e4300;--bad-deep:#930e00}
 /* Only "system" asks the OS, so an explicit choice always wins: dark and light
    are attribute values this query cannot match. The light block is written
    twice because CSS has no way to alias a rule into a media query, and the
    alternative - resolving the OS preference in script - is the repaint the
    pre-paint line above exists to avoid. */
 @media (prefers-color-scheme:light){
  :root[data-theme="system"]{color-scheme:light;--bg:#ffffff;--panel:#f8f8f8;--raised:#f3f3f3;--line:#e5e5e5;--edge:#cecece;
       --ink:#3b3b3b;--dim:#616161;--field:#ffffff;--hover:#f0f0f0;
       --accent:#005fb8;--accent-hover:#0258a8;--on-accent:#ffffff;--link:#005fb8;
       --ok:#1a7f37;--bad:#d81400;--warn:#8a6200;
       --warn-deep:#5e4300;--bad-deep:#930e00}}
 *{box-sizing:border-box}
 /* Keyboard focus has to be visible on either ground, and the accent is the one
    token that is legible on both. */
 :focus-visible{outline:2px solid var(--accent);outline-offset:1px}
 body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:0 1rem 3rem;
      max-width:70rem;margin-inline:auto;line-height:1.45}
 header{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;padding:1.2rem 0 .6rem}
 /* Accent as TEXT is always --link: #0078d4 on #1f1f1f is about 3.0:1, which is
    a button color, not a reading color. Accent as a fill stays --accent. */
 h1{color:var(--link);font-size:1.25rem;margin:0;letter-spacing:.02em}
 /* Sized off the type and not off the header, so the mark stays a lockup with
    the wordmark instead of a banner: at .9em the artwork's own ink lands about
    on the h1's cap height. The negative vertical-align is the baseline fix - an
    inline SVG rests its BOTTOM EDGE on the baseline, and this viewBox carries
    empty space under its lowest ink, so at the default alignment the whole mark
    floats above the text it is set against. */
 h1 .mark{width:.9em;height:.9em;vertical-align:-.13em;margin-right:.38em}
 #summary{color:var(--dim);font-size:.85rem}
 #theme{width:auto;margin-left:auto;font-size:.8rem;padding:.25rem .4rem}
 nav{display:flex;gap:.25rem;border-bottom:1px solid var(--line);margin-bottom:1.1rem;flex-wrap:wrap}
 nav button{background:none;border:0;border-bottom:2px solid transparent;color:var(--dim);cursor:pointer;
            padding:.55rem .85rem;font:inherit;font-size:.9rem}
 nav button.on{color:var(--link);border-bottom-color:var(--accent)}
 section{display:none} section.on{display:block}
 table{width:100%;border-collapse:collapse;font-size:.85rem}
 td,th{border-bottom:1px solid var(--line);padding:.45rem .5rem;text-align:left;vertical-align:top}
 th{color:var(--dim);font-weight:500;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
 .done{color:var(--ok)} .failed{color:var(--bad)} .running{color:var(--link)} .dim{color:var(--dim)}
 .queued,.cancelled{color:var(--dim)}
 code{color:var(--dim);font-size:.75rem;word-break:break-all}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:1rem;margin-bottom:1rem}
 .card h2{font-size:.95rem;margin:0 0 .2rem;color:var(--ink)}
 .card p.hint{color:var(--dim);font-size:.8rem;margin:.1rem 0 .9rem}
 label{display:block;margin-bottom:.9rem;font-size:.85rem}
 label .lab{display:flex;justify-content:space-between;align-items:center;gap:.5rem;margin-bottom:.25rem}
 label small{color:var(--dim);display:block;font-size:.76rem;font-weight:400;margin-top:.15rem}
 input[type=text],input[type=number],input[type=password],textarea,select{
   width:100%;background:var(--field);border:1px solid var(--edge);border-radius:6px;color:var(--ink);
   padding:.45rem .55rem;font:inherit;font-size:.85rem}
 textarea{min-height:4.5rem;resize:vertical}
 input[type=checkbox]{width:auto;transform:scale(1.15);margin-right:.4rem}
 button.act{background:var(--accent);color:var(--on-accent);border:0;border-radius:6px;padding:.45rem .9rem;
            font:inherit;font-size:.85rem;font-weight:600;cursor:pointer}
 button.act:hover:not(:disabled){background:var(--accent-hover)}
 button.ghost{background:none;border:1px solid var(--line);color:var(--dim);border-radius:6px;
              padding:.35rem .7rem;font:inherit;font-size:.8rem;cursor:pointer}
 /* currentColor rather than a per-state border token: every one of these already
    sets the color the border was hand-picked to match, in both themes. */
 button.danger{border-color:currentColor;color:var(--bad)}
 /* A test is seconds of real ffmpeg. The button has to look spent while it runs. */
 button:disabled{opacity:.5;cursor:progress}
 .row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
 .tag{font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;padding:.1rem .4rem;border-radius:4px;
      border:1px solid var(--line);color:var(--dim)}
 .tag.stored{color:var(--link);border-color:currentColor} .tag.env{color:var(--warn);border-color:currentColor}
 .msg{padding:.5rem .7rem;border-radius:6px;font-size:.83rem;margin-bottom:.8rem;display:none}
 /* The state is the left edge, not the text. A filled message box is a tinted
    surface, and the light error red reads at about 4.3:1 as small text on one -
    an edge carries the same signal at any contrast. */
 .msg.ok,.msg.err{display:block;background:var(--raised);color:var(--ink);border-left:3px solid var(--ok)}
 .msg.err{border-left-color:var(--bad)}
 .browser{max-height:16rem;overflow:auto;border:1px solid var(--line);border-radius:6px}
 .browser div{padding:.3rem .55rem;cursor:pointer;font-size:.82rem;border-bottom:1px solid var(--line)}
 .browser div:hover{background:var(--hover);color:var(--link)}
 .pill{display:inline-flex;align-items:center;gap:.4rem;background:var(--raised);border:1px solid var(--line);
       border-radius:999px;padding:.2rem .3rem .2rem .7rem;font-size:.8rem;margin:0 .4rem .4rem 0}
 .pill button{background:none;border:0;color:var(--dim);cursor:pointer;font-size:.95rem;line-height:1}
 /* Neutral, not green: this box shows a failed profile test as often as a key. */
 .keyout{background:var(--raised);border:1px solid var(--edge);border-radius:6px;padding:.7rem;margin-bottom:.9rem;
         font-size:.82rem;display:none}
 .keyout code{color:var(--ok);font-size:.85rem;user-select:all}
 .now{background:linear-gradient(180deg,var(--raised),var(--panel));border:1px solid var(--edge)}
 .now .file{font-size:1rem;color:var(--ink);word-break:break-word;margin:.1rem 0 .1rem}
 .now .where{color:var(--dim);font-size:.78rem;word-break:break-all;margin-bottom:.7rem}
 /* Track is the darker step and the outline the lighter one, not the reverse:
    the bar's meaning is where the fill stops, and accent on --edge was 2.4:1 in
    dark. On --line it is 3.1:1, and the outline keeps the empty track findable. */
 .bar{height:8px;background:var(--line);border-radius:999px;overflow:hidden;border:1px solid var(--edge)}
 .bar i{display:block;height:100%;background:linear-gradient(90deg,var(--accent-hover),var(--accent));transition:width .8s linear}
 .facts{display:flex;gap:1.4rem;flex-wrap:wrap;color:var(--dim);font-size:.8rem;margin-top:.6rem}
 .facts b{color:var(--ink);font-weight:600}
 .idle{color:var(--dim);font-size:.9rem;padding:.4rem 0}
 /* Encoding tab */
 .hero{display:flex;gap:1.6rem;flex-wrap:wrap;align-items:center}
 .hero .big{font-size:1.5rem;color:var(--link);font-weight:600;letter-spacing:.01em}
 .hero .sub{color:var(--dim);font-size:.8rem}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(16rem,1fr));gap:.8rem}
 .enc{background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:.8rem;position:relative;
      display:flex;flex-direction:column;gap:.35rem}
 .enc.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
 .enc.off{opacity:.5}
 .enc .top{display:flex;align-items:center;gap:.5rem;justify-content:space-between}
 .enc .nm{font-weight:600;color:var(--ink);font-size:.92rem}
 .enc .why{color:var(--dim);font-size:.76rem;line-height:1.35}
 .dot{width:.5rem;height:.5rem;border-radius:50%;display:inline-block;flex:none}
 /* No glow: it was a dark-theme flourish that reads as a smudge on a white page. */
 .dot.y{background:var(--ok)} .dot.n{background:var(--bad)}
 .chips{display:flex;gap:.3rem;flex-wrap:wrap}
 .chip{font-size:.66rem;text-transform:uppercase;letter-spacing:.05em;padding:.08rem .38rem;border-radius:4px;
       border:1px solid var(--line);color:var(--dim)}
 .chip.gpu{color:var(--ok);border-color:currentColor} .chip.cpu{color:var(--warn);border-color:currentColor}
 .enc .act{margin-top:.4rem;align-self:flex-start}
 .enc .using{color:var(--link);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em}
 .hwline{display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap;font-size:.83rem;
         padding:.3rem 0;border-top:1px solid var(--line)}
 .hwline.off{opacity:.55} .hwline .why{color:var(--dim);font-size:.76rem}
 .card h3{font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);
          font-weight:500;margin:1.2rem 0 .5rem}
 input[type=range]{width:100%;accent-color:var(--accent)}
 .scale{display:flex;justify-content:space-between;color:var(--dim);font-size:.72rem;margin-top:.15rem}
 .qnum{font-size:1.3rem;color:var(--link);font-weight:600;font-variant-numeric:tabular-nums}
 .two{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:0 1rem}
 .meters{display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));gap:.9rem}
 .meter .mtop{display:flex;justify-content:space-between;align-items:baseline;font-size:.78rem;color:var(--dim)}
 .meter .mval{font-size:1.05rem;color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
 .meter .bar{margin-top:.3rem}
 .meter .sub{color:var(--dim);font-size:.72rem;margin-top:.25rem;line-height:1.3}
 .bar i.warn{background:linear-gradient(90deg,var(--warn-deep),var(--warn))}
 .bar i.bad{background:linear-gradient(90deg,var(--bad-deep),var(--bad))}
 td.pos{color:var(--dim);width:2.5rem;text-align:right;font-variant-numeric:tabular-nums}
 tr.next td{color:var(--link)}
 /* The sign-in screen covers the page rather than sitting inside it: everything
    behind it needs a token to have rendered at all. */
 #gate{position:fixed;inset:0;background:var(--bg);z-index:20;padding:1rem;
       display:none;align-items:center;justify-content:center}
 #gate.on{display:flex}
 .gatebox{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:1.3rem;
          width:min(23rem,100%)}
 .gatebox h1{margin-bottom:.7rem}
 .gatebox button.act{width:100%}
 #firstrun{display:none}
</style>
<div id="gate">
 <div class="gatebox">
  <h1>TranscodeArr</h1>
  <p class="hint" id="gatehint">&nbsp;</p>
  <div class="msg" id="gatemsg"></div>
  <div id="gatelogin">
   <label><span class="lab">Username</span><input type="text" id="g_user" autocomplete="username"></label>
   <label><span class="lab">Password</span><input type="password" id="g_pass" autocomplete="current-password"></label>
   <button class="act" id="g_signin" onclick="signIn(this)">Sign in</button>
  </div>
  <div id="gatetoken">
   <label><span class="lab">API token</span><input type="password" id="g_token" autocomplete="off">
    <small>The token set in the container's environment, or a key minted on the Access tab.</small></label>
   <button class="act" id="g_usetoken" onclick="useToken(this)">Use this token</button>
  </div>
  <p class="hint" style="margin:.9rem 0 0"><button class="ghost" id="g_swap" onclick="gateMode()"></button></p>
 </div>
</div>
<header><h1>""" + _INLINE_MARK + r"""TranscodeArr</h1><div id="summary">connecting...</div>
 <select id="theme" aria-label="Color theme" onchange="setTheme(this.value)"><option value="dark">Dark</option>
  <option value="light">Light</option><option value="system">System</option></select>
 <button class="ghost" onclick="signOut(this)">Sign out</button></header>
<div class="msg err" id="firstrun">No admin account exists yet, so this box has no login form and its API token is
 the only way in. <button class="ghost" onclick="gotoTab('keys')">Create an account</button></div>
<nav>
 <button data-tab="queue" class="on">Queue</button>
 <button data-tab="jobs">History</button>
 <button data-tab="locations">Locations</button>
 <button data-tab="hardware">Encoding</button>
 <button data-tab="rules">Rules</button>
 <button data-tab="connections">Connections</button>
 <button data-tab="system">System</button>
 <button data-tab="keys">Access</button>
</nav>

<section id="queue" class="on">
 <div class="card now" id="runcard">
  <div class="msg" id="runmsg"></div>
  <div class="hero" style="justify-content:space-between">
   <div><div class="big" id="runbig">-</div><div class="sub" id="runwhy">&nbsp;</div></div>
   <div class="row">
    <button class="act" id="stopbtn" onclick="runControl('stop',this)">Stop converting</button>
    <button class="act" id="startbtn" onclick="runControl('start',this)">Start converting</button>
   </div>
  </div>
  <div class="facts" id="runwindow"></div>
  <p class="hint" style="margin:.8rem 0 0">Stopping drains: the file already encoding finishes, is verified and is
   revealed - a 40GB remux at 90% is never thrown away. The watcher keeps queueing either way, so a stopped box
   with a growing queue is working exactly as intended.</p>
 </div>
 <div class="card"><h2>Host</h2>
  <p class="hint" id="hostnote">&nbsp;</p>
  <div class="meters" id="host"></div>
 </div>
 <div class="card now" id="nowcard"><h2>Converting now</h2><div id="now"></div></div>
 <div class="card">
  <h2>Up next</h2>
  <p class="hint" id="queuehint">In the order they will run - one at a time, oldest first.</p>
  <div class="msg" id="scanmsg"></div>
  <div class="row" style="margin-bottom:.8rem">
   <button class="ghost" id="scanbtn" onclick="scanNow(this)">Check for files to convert</button>
   <span class="hint">The watcher does this on its own every scan interval. This is the same walk, now.</span>
  </div>
  <table id="queuetable"></table>
 </div>
</section>

<section id="jobs">
 <div class="row" style="margin-bottom:.8rem">
  <select id="statefilter" style="width:auto">
   <option value="">All states</option><option value="queued">Queued</option>
   <option value="running">Running</option><option value="done">Done</option>
   <option value="failed">Failed</option><option value="cancelled">Cancelled</option>
  </select>
  <button class="ghost" onclick="refreshJobs()">Refresh</button>
 </div>
 <table id="jobtable"></table>
</section>

<section id="locations">
 <div class="msg" id="locmsg"></div>
 <div class="card">
  <h2>Watched folders</h2>
  <p class="hint">Scanned for files to convert: every matching video file, or only those hidden behind a
   leading dot if "Only convert dot-hidden files" below is on. With none set, every media root is
   watched, so everything under the mounts is eligible on the first scan.</p>
  <div id="watchpills"></div>
  <div class="row"><input type="text" id="watchadd" placeholder="/media/TV">
   <button class="act" onclick="addWatch()">Add</button></div>
  <p class="hint" style="margin-top:.8rem">Browse the paths this container can actually see:</p>
  <div class="row"><code id="browsepath">/</code><button class="ghost" onclick="browse(upOne())">Up</button></div>
  <div class="browser" id="browser"></div>
 </div>
 <div class="card"><h2>Scanning</h2><div id="locform"></div>
  <button class="act" onclick="saveSettings('locform','locmsg')">Save</button></div>
</section>

<section id="hardware">
 <div class="msg" id="hwmsg"></div>
 <div class="card">
  <h2>Hardware</h2>
  <div class="hero" id="hwhero"></div>
  <p class="hint" style="margin-top:.9rem">Each encoder was tried with a real one-second encode. Being listed by
   ffmpeg proves nothing - a card that cannot do AV1 still advertises it, and NVENC advertises itself with no
   driver loaded at all. Nothing here is a choice; pick a profile below instead.
   <button class="ghost" onclick="probeHW(this)" style="margin-left:.4rem">Re-test hardware</button></p>
  <div id="hwsum"></div>
 </div>
 <div class="card">
  <h2>Profiles</h2>
  <p class="hint">A profile is every output choice in one bundle. The one marked default drives every job;
   switching is one click. Files already converted are untouched.
   <button class="ghost" onclick="retestProfiles(this)" style="margin-left:.4rem">Re-test all</button>
   re-encodes a test clip once per profile, so give it about a minute.</p>
  <h3>Shipped profiles</h3>
  <p class="hint">One per encoder, and not editable. Duplicate one to make a version you can change.</p>
  <div class="grid" id="profshipped"></div>
  <h3>Your profiles</h3>
  <div class="grid" id="profmine"></div>
 </div>
 <div class="card">
  <h2 id="profedittitle">New profile</h2>
  <p class="hint">Only encoders that actually work on this machine are offered, and nothing is saved until a
   real two-second encode with these exact settings succeeds - the settings that fail are the ones that look
   perfectly reasonable in a form.</p>
  <label><span class="lab">Name</span><input type="text" id="p_name" placeholder="e.g. 1080p Space Saver"></label>
  <div class="two">
   <label><span class="lab">Encoder</span><select id="p_encoder"></select><small id="p_enchelp"></small></label>
   <label><span class="lab">Resolution</span><select id="p_res"></select><small id="p_reshelp"></small></label>
  </div>
  <label><span class="lab"><span>Quality</span><span class="qnum" id="p_qval">-</span></span>
   <input type="range" id="p_quality" min="14" max="34" step="1">
   <div class="scale"><span>smaller files</span><span id="p_qrec"></span><span>better picture</span></div></label>
  <div class="two">
   <label><span class="lab">Speed vs size</span><select id="p_preset"></select>
    <small>Slower settings spend longer to make a smaller file at the same quality.</small></label>
   <label><span class="lab">Codec profile</span><select id="p_profile"></select>
    <small>How modern a decoder the file expects.</small></label>
  </div>
  <div class="two">
   <label><span class="lab">Audio</span><select id="p_acodec"></select></label>
   <label><span class="lab">Channels</span><select id="p_achan"></select>
    <small>Source keeps a 5.1 mix intact.</small></label>
  </div>
  <label id="p_abr_wrap"><span class="lab">Audio bitrate (kbps)</span>
   <input type="number" id="p_abitrate" min="32" max="640" step="16"></label>
  <div class="keyout" id="p_testout"></div>
  <div class="row">
   <button class="ghost" onclick="testProfile(this)">Test this profile</button>
   <button class="act" onclick="saveProfile(this)">Save</button>
   <button class="ghost" onclick="resetProfileForm()">Clear</button>
  </div>
 </div>
</section>

<section id="rules">
 <div class="msg" id="rulemsg"></div>
 <div class="card"><h2>Conversion rules</h2>
  <p class="hint">Applied to every job from now on. Files already converted are unaffected.</p>
  <div id="ruleform"></div>
  <button class="act" onclick="saveSettings('ruleform','rulemsg')">Save</button></div>
 <div class="card"><h2>Housekeeping</h2><div id="genform"></div>
  <button class="act" onclick="saveSettings('genform','genmsg')">Save</button>
  <div class="msg" id="genmsg"></div></div>
</section>

<section id="connections">
 <div class="msg" id="arrmsg"></div>
 <div class="card">
  <h2>Radarr / Sonarr</h2>
  <p class="hint">After a file is replaced, the arr that owns it is asked to re-read it - otherwise the whole
   stack keeps reporting the old file's codec and runtime. Path mapping matters: this container and the arr
   mount the same folder at different places.</p>
  <table id="arrtable"></table>
 </div>
 <div class="card"><h2 id="arrformtitle">Add a connection</h2><div id="arrform"></div>
  <div class="row"><button class="act" onclick="saveArr()">Save</button>
   <button class="ghost" onclick="testArr()">Test</button>
   <button class="ghost" onclick="resetArrForm()">Clear</button></div></div>
</section>

<section id="system">
 <div class="msg" id="sysmsg"></div>
 <div class="card"><h2>Schedule</h2>
  <p class="hint">The window is read in the CONTAINER's timezone, which is UTC unless TZ is set in the compose
   file. The clock beside the window on the Queue tab is that timezone - check it matches the box you are sitting
   at before trusting a window you typed.</p>
  <div id="schedform"></div>
  <button class="act" onclick="saveSettings('schedform','sysmsg')">Save</button></div>
 <div class="card"><h2>Throttling</h2>
  <p class="hint">Applies to the next job started, not the one running now.</p>
  <div id="perfform"></div>
  <button class="act" onclick="saveSettings('perfform','sysmsg')">Save</button></div>
 <div class="card"><h2>Webhook</h2>
  <p class="hint">One POST per finished job, done or failed. Nothing about it can fail a job or slow one down.</p>
  <div id="hookform"></div>
  <button class="act" onclick="saveSettings('hookform','sysmsg')">Save</button></div>
 <div class="card"><h2>Security</h2>
  <p class="hint">A reverse proxy that already terminates TLS is the better answer; this is for a LAN box with no
   proxy. Both paths take effect on the next restart, and a certificate that is set but unreadable stops the
   container on purpose rather than quietly serving your password in clear text.</p>
  <div id="secform"></div>
  <button class="act" onclick="saveSettings('secform','sysmsg')">Save</button>
  <h3>Self-signed certificate</h3>
  <p class="hint">For a LAN box with no certificate authority. Written into the config volume, never over an
   existing pair. Browsers warn once and then remember it.</p>
  <div class="row"><input type="text" id="tlshost" placeholder="nas.local or 192.168.1.10" style="max-width:18rem">
   <button class="ghost" onclick="selfSign(this)">Generate</button></div>
  <div class="keyout" id="tlsout"></div></div>
 <div class="card"><h2>Backup and restore</h2>
  <p class="hint">Settings, profiles and connections as one file. Secrets never leave: no arr API keys, no webhook
   signing secret, no password or token hashes. Job history and the trash are not configuration and are not in it.</p>
  <div class="row">
   <button class="act" onclick="downloadBackup(this)">Download backup</button>
   <input type="file" id="restorefile" accept=".json,application/json" style="max-width:18rem">
   <button class="ghost" onclick="restoreBackup(this)">Restore this file</button>
  </div>
  <div class="keyout" id="restoreout"></div></div>
</section>

<section id="keys">
 <div class="msg" id="keymsg"></div>
 <div class="card" id="admincard">
  <h2 id="admintitle">Admin account</h2>
  <p class="hint" id="adminhint">&nbsp;</p>
  <div class="two">
   <label><span class="lab">Username</span><input type="text" id="ad_user" autocomplete="username"></label>
   <label id="ad_cur_wrap"><span class="lab">Current password</span>
    <input type="password" id="ad_cur" autocomplete="current-password">
    <small>Needed for a rename too. An API key alone must not be able to lock you out of your own box.</small></label>
  </div>
  <label><span class="lab">New password</span><input type="password" id="ad_pass" autocomplete="new-password">
   <small>Eight characters or more. Kept as a scrypt hash with its own salt - the password itself is never
    stored, never logged and never sent back.</small></label>
  <button class="act" onclick="saveAdmin(this)">Save</button>
 </div>
 <div class="card"><h2>Signed-in browsers</h2>
  <p class="hint">One row per password login. Revoking one signs that browser out on its next request. API keys
   are not sessions and are listed below.</p>
  <table id="sesstable"></table></div>
 <div class="keyout" id="keyout"></div>
 <div class="card"><h2>API keys</h2>
  <p class="hint">A key is shown once, when it is created, and only its hash is stored. The token set in the
   container's environment keeps working as a bootstrap key and cannot be revoked from here.</p>
  <div class="row"><input type="text" id="keyname" placeholder="e.g. ManageArr">
   <button class="act" onclick="mintKey()">Create key</button></div>
  <table id="keytable" style="margin-top:.9rem"></table>
 </div>
</section>

<script>
const $=id=>document.getElementById(id);
// Separators here are literal characters, never HTML entities. esc() escapes
// the & in "&middot;" to "&amp;middot;", so a separator joined into a string
// BEFORE escaping renders as the text "&middot;" - which is exactly what the
// CPU line showed on a real box. A literal character survives either side.
const esc=s=>String(s??'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
// A session token and a minted API key are held the same way and sent the same
// way. Nothing here knows which it has, which is the point: no cookie, so no
// request this page did not make can carry it.
const token=()=>localStorage.token||'';
let SPECS=[],SET={},SRC={},editingArr=null,timer=null,ADMIN=false;
// Files the last scan threw away for being visible, from the run state. Read by
// the queue panel, which is where somebody stares when nothing ever converts.
// Both refreshers run from the same tick and health goes first, so this is
// always the current scan's number by the time the queue renders.
let VISIBLEONLY=0;

function locked(msg){
 // A mistyped token used to sit in localStorage forever while the page stayed
 // blank and nothing said why. Forget it, say so, stop polling.
 delete localStorage.token; if(timer) clearInterval(timer); timer=null;
 $('summary').textContent='signed out';
 showGate(msg);
}
async function api(path,opts={}){
 const r=await fetch(path,{...opts,headers:{'Content-Type':'application/json',
   Authorization:'Bearer '+token(),...(opts.headers||{})}});
 if(r.status===401){locked('Signed out - that session or token is no longer accepted.');
  throw new Error('unauthorized');}
 const body=r.status===204?null:await r.json().catch(()=>null);
 if(!r.ok) throw new Error((body&&body.error)||('HTTP '+r.status));
 return body;
}
function say(el,text,bad){const n=$(el);n.className='msg '+(bad?'err':'ok');n.textContent=text;
 setTimeout(()=>{n.className='msg';},4000);}
// Anything that probes or tests spends seconds inside a real ffmpeg run. A page
// that just sits there reads as broken, so the button says what it is doing -
// and comes back in the finally, because the failing test is the one you most
// need to be able to press again.
async function busy(btn,label,fn){
 if(!btn) return fn();
 const was=btn.textContent;
 btn.disabled=true;btn.textContent=label;
 try{return await fn();}finally{btn.disabled=false;btn.textContent=was;}
}
// A declaration and not a const arrow: this one is called from an inline
// onclick in the markup, which resolves names off the global object.
function gotoTab(name){document.querySelector('nav button[data-tab='+name+']').click();}

// ---- signing in ----------------------------------------------------------
let GATEMODE='login';
function gsay(m){const n=$('gatemsg');n.className=m?'msg err':'msg';n.textContent=m||'';}
function gateMode(m){
 GATEMODE=m||(GATEMODE==='login'?'token':'login');
 const login=GATEMODE==='login';
 $('gatelogin').style.display=login?'block':'none';
 $('gatetoken').style.display=login?'none':'block';
 // The token door stays open beside the password one on purpose: it is the only
 // way back in for somebody who has forgotten the password and still has the
 // bootstrap token in their compose file.
 $('g_swap').textContent=login?'Sign in with an API token instead':'Sign in with a username and password';
 $('g_swap').style.display=ADMIN?'inline-block':'none';
 $('gatehint').textContent=ADMIN?'':
  "No admin account exists yet. Sign in with the API token from this container's environment, then create one "+
  "on the Access tab.";
 $(login?'g_user':'g_token').focus();
}
async function showGate(msg){
 if(timer) clearInterval(timer); timer=null;
 // /healthz is the one route with no token on it, and admin_configured is why
 // the page asks it here: which form to draw is decided by whether this box has
 // a password, not by what it had when the tab was opened.
 try{ADMIN=!!(await (await fetch('/healthz')).json()).admin_configured;}catch(e){}
 $('gate').classList.add('on');
 gateMode(ADMIN?'login':'token');
 gsay(msg||'');
}
function hideGate(){$('gate').classList.remove('on');gsay('');}
async function signIn(btn){
 const username=$('g_user').value.trim(),password=$('g_pass').value;
 if(!username||!password) return gsay('Type a username and a password.');
 await busy(btn,'Signing in...',async()=>{
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username,password})});
  const b=await r.json().catch(()=>null);
  if(r.status===409){ADMIN=false;gateMode('token');
   return gsay('This box has no password yet - sign in with its API token and create an account.');}
  // Shown exactly as the server phrased it. One message covers a wrong username
  // and a wrong password, so this form cannot be asked which accounts exist,
  // and the 429 carries its own wait in the same field.
  if(!r.ok) return gsay((b&&b.error)||('Sign in failed - HTTP '+r.status));
  localStorage.token=b.token;$('g_pass').value='';
  hideGate();boot();
 });
}
async function useToken(btn){
 const t=$('g_token').value.trim();
 if(!t) return gsay('Paste a token first.');
 await busy(btn,'Checking...',async()=>{
  // Checked with a bare fetch, not api(): api()'s own 401 handler would clear
  // the token and re-open this screen underneath the message being written.
  const r=await fetch('/api/settings',{headers:{Authorization:'Bearer '+t}});
  if(!r.ok) return gsay(r.status===401?'That token was not accepted.':'HTTP '+r.status);
  localStorage.token=t;$('g_token').value='';
  hideGate();boot();
 });
}
async function signOut(btn){
 await busy(btn,'Signing out...',async()=>{
  // An API key answers 400 here and is deliberately not revoked: a button
  // labeled "sign out" must not cut off an integration. Either way
  // this browser forgets what it was holding, which is what was asked for.
  try{await api('/api/logout',{method:'POST',body:'{}'});}catch(e){}
  delete localStorage.token;
  location.reload();
 });
}

document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
 document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id===b.dataset.tab));
 // Refresh on arrival rather than showing whatever the last poll left behind.
 if(b.dataset.tab==='queue'){refreshQueue().catch(()=>{});refreshHost().catch(()=>{});}
 if(b.dataset.tab==='jobs') refreshJobs().catch(()=>{});
 if(b.dataset.tab==='hardware') loadProfiles().then(loadHW).catch(()=>{});
 if(b.dataset.tab==='keys'){loadKeys().catch(()=>{});loadSessions().catch(()=>{});}
});
['p_encoder','p_quality','p_res','p_acodec'].forEach(id=>{
 document.addEventListener('change',e=>{
  if(e.target.id==='p_encoder') onEncoderChange(null);
  if(e.target.id==='p_res') onResChange();
  if(e.target.id==='p_acodec') onAudioChange();
 });
 document.addEventListener('input',e=>{ if(e.target.id==='p_quality') onQualityChange(); });
});

// ---- the queue: what is running, and what is next in running order -------
const fileOf=p=>String(p||'').split('/').pop();
const folderOf=p=>String(p||'').split('/').slice(0,-1).join('/');
function dur(s){
 if(s==null||!isFinite(s)) return '';
 s=Math.max(0,Math.round(s));
 const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
 if(d) return `${d}d ${h}h`;
 if(h) return `${h}h ${m}m`;
 if(m) return `${m}m ${s%60}s`;
 return `${s}s`;
}
const gb=b=>(b/1073741824).toFixed(1)+' GB';
function meter(label,value,pct,sub){
 const p=pct==null?null:Math.max(0,Math.min(100,pct));
 const cls=p==null?'':p>=90?'bad':p>=75?'warn':'';
 return `<div class="meter"><div class="mtop"><span>${esc(label)}</span><span class="mval">${esc(value)}</span></div>
  <div class="bar"><i class="${cls}" style="width:${p==null?0:p}%"></i></div>
  <div class="sub">${sub||''}</div></div>`;
}
async function refreshHost(){
 let s;
 try{s=await api('/api/system');}catch(e){return;}
 $('hostnote').textContent=s.note||'';
 const out=[];
 if(s.cpu) out.push(meter('CPU',s.cpu.percent==null?'-':s.cpu.percent+'%',s.cpu.percent,
   esc([s.cpu.model,(s.cpu.cores?s.cpu.cores+' threads':'')].filter(Boolean).join(' · '))));
 if(s.load) out.push(meter('Load',s.load.one.toFixed(2),s.load.per_core*100,
   `${s.load.per_core} per thread · 5m ${s.load.five.toFixed(2)} · 15m ${s.load.fifteen.toFixed(2)}`
   +(s.load.per_core>1?' <span style="color:var(--warn)">- more work queued than threads</span>':'')));
 if(s.memory) out.push(meter('Memory',s.memory.percent+'%',s.memory.percent,
   `${gb(s.memory.used)} of ${gb(s.memory.total)} used · ${gb(s.memory.available)} available`));
 if(s.gpu){
  out.push(meter('GPU',(s.gpu.percent??'-')+'%',s.gpu.percent,
   `${esc(s.gpu.name||'')}${s.gpu.temperature_c!=null?' · '+s.gpu.temperature_c+'°C':''}`));
  const sess=s.gpu.encoder_sessions;
  out.push(meter('Encoder',sess==null?'-':sess+(sess===1?' session':' sessions'),
   sess==null?null:Math.min(100,sess*25),
   (s.gpu.encoder_fps?s.gpu.encoder_fps+' fps encoded':'idle')
   +` · ${s.converting} job${s.converting===1?'':'s'} of ${s.max_concurrent} allowed`));
  if(s.gpu.memory_total_mb) out.push(meter('GPU memory',
    Math.round(s.gpu.memory_used_mb)+' MB',
    100*s.gpu.memory_used_mb/s.gpu.memory_total_mb,
    `of ${Math.round(s.gpu.memory_total_mb)} MB`));
 } else if(s.other_gpu){
  out.push(meter('GPU','present',null,esc(s.other_gpu)+' - utilization needs vendor tools this container does not carry'));
 }
 $('host').innerHTML=out.join('');
}

async function refreshQueue(){
 const q=await api('/api/queue?limit=200');
 const r=(q.running||[])[0];
 if(r){
  const pct=Math.max(0,Math.min(100,r.progress||0));
  const elapsed=r.started?(Date.now()/1000-r.started):null;
  // Remaining is extrapolated from THIS file's own progress, not the fleet
  // average - a 20-minute episode behind a 2-hour film would otherwise read
  // as an hour out.
  const left=(pct>2&&elapsed)?elapsed*(100-pct)/pct:null;
  $('now').innerHTML=
   `<div class="file">${esc(fileOf(r.path))}</div>
    <div class="where">${esc(folderOf(r.path))}</div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    <div class="facts"><span><b>${pct}%</b></span>
     <span>encoder <b>${esc(r.encoder||'?')}</b></span>
     ${elapsed?`<span>running <b>${dur(elapsed)}</b></span>`:''}
     ${left?`<span>about <b>${dur(left)}</b> left</span>`:''}
     <span><button class="ghost" data-cancel="${esc(r.id)}">Cancel</button></span></div>`;
 } else {
  $('now').innerHTML='<div class="idle">Nothing converting right now.</div>';
 }

 const rows=q.queued||[];
 $('queuetable').innerHTML='<tr><th class="pos">#</th><th>File</th><th>Folder</th><th>Type</th><th></th></tr>'+
  (rows.length?rows.map((x,i)=>
    `<tr class="${i===0?'next':''}"><td class="pos">${i+1}</td><td>${esc(fileOf(x.path))}</td>`+
    `<td><code>${esc(folderOf(x.path))}</code></td>`+
    `<td>${x.kind==='reveal'?'<span class="tag">reveal only</span>':'transcode'}</td>`+
    `<td><button class="ghost" data-cancel="${esc(x.id)}">Cancel</button></td></tr>`).join('')
   :'<tr><td colspan="5">Queue is empty.</td></tr>');

 const shown=rows.length,total=q.queued_total||0,at=q.max_concurrent||1;
 const reveals=rows.filter(x=>x.kind==='reveal').length;
 let hint=total?`${total} waiting${shown<total?` (showing the next ${shown})`:''} - `+
   `${at>1?at+' at a time':'one at a time'}, files needing no conversion first, then oldest first.`
              :'Nothing waiting.';
 if(reveals) hint+=` ${reveals} of these need no conversion and will be revealed straight away.`;
 if(q.seconds_per_job&&total)
  hint+=` About ${dur(q.seconds_per_job)} each lately, so roughly ${dur(q.eta_seconds)} to clear.`;
 // An empty queue that will STAY empty reads exactly like an idle one, and this
 // panel is the only place anybody looks. The server has already counted the
 // files it rejected, so say which of the two this is.
 if(!total&&VISIBLEONLY)
  hint=`Nothing waiting, and nothing will be: the watched folders hold ${VISIBLEONLY} `+
   `video file${VISIBLEONLY===1?'':'s'} that would be converted, and not one of them is hidden behind a `+
   'leading dot. Only dot-hidden files are eligible while "Only convert dot-hidden files" is on. Turn '+
   'that off from the Locations tab (it makes every visible file in the watched folders eligible, so '+
   'read what it says first), or have whatever imports your media write each file with a leading dot '+
   'for this worker to reveal.';
 $('queuehint').textContent=hint;
}
// Every action is delegated off a data attribute holding only an id, so no
// server-supplied text - a file name, an arr name, or an error message an arr
// itself chose - is ever interpolated into a JavaScript string literal or an
// inline handler. esc() escapes HTML, not JS string context, and the two are
// not the same escape.
document.addEventListener('click',e=>{
 const d=e.target&&e.target.dataset;
 if(!d) return;
 if(d.cancel) cancelJob(d.cancel);
 if(d.editarr){const a=ARRS.find(x=>x.id===d.editarr); if(a) editArr(a);}
 if(d.delarr){const a=ARRS.find(x=>x.id===d.delarr); if(a) deleteArr(a.id,a.name);}
 if(d.revoke){const t=KEYS.find(x=>x.id===d.revoke); if(t) revokeKey(t.id,t.name);}
 if(d.revokesession) revokeSession(d.revokesession);
 if(d.browse!==undefined) browse(d.browse);
 if(d.addwatch) addBrowsed();
 if(d.useprof) useProfile(d.useprof);
 if(d.testprof) testStoredProfile(d.testprof,e.target);
 if(d.editprof){const p=profBy(d.editprof);
  if(p){editingProfile=p.id;fillProfileForm(p);window.scrollTo(0,document.body.scrollHeight);}}
 if(d.dupprof){const p=profBy(d.dupprof); if(p) duplicateProfile(p);}
 if(d.delprof){const p=profBy(d.delprof); if(p) deleteProfile(p.id,p.name);}
 if(d.dropwatch!==undefined) dropWatch(Number(d.dropwatch));
});

// ---- run state: converting now, or why not -------------------------------
// The buttons are in the markup and only toggled, never re-rendered: this line
// repaints every poll, and replacing a button under a click loses the disabled
// state busy() just put on it.
function renderRun(z){
 const on=z.run_state==='running';
 VISIBLEONLY=z.visible_only_skipped||0;
 $('runbig').textContent=z.converting?'Converting':on?'Waiting for the window':'Stopped';
 $('runbig').style.color=z.converting?'var(--ok)':on?'var(--warn)':'var(--dim)';
 // The server's own sentence, shown verbatim - it is the same string the log
 // prints, so a screenshot and a log line say the same thing.
 $('runwhy').textContent=z.reason||'';
 $('startbtn').style.display=on?'none':'inline-block';
 $('stopbtn').style.display=on?'inline-block':'none';
 // The zone travels with the window everywhere the window appears. A container
 // is UTC unless TZ says otherwise and the NAS under it usually is not, so a
 // window typed as 01:00-06:00 runs four hours out with nothing on screen to
 // say so - showing the zone and the clock is what makes that visible.
 const bits=[`<span>local time <b>${esc(z.local_time||'')}</b></span>`,
   `<span>TZ <b>${esc(z.timezone||'unset, so this container is on UTC')}</b></span>`];
 if(z.convert_window){
  bits.unshift(`<span>window <b>${esc(z.convert_window)}</b></span>`);
  if(z.next_change_seconds!=null)
   bits.push(`<span>${z.converting?'closes':'opens'} in <b>${dur(z.next_change_seconds)}</b></span>`);
 } else bits.unshift('<span>window <b>any time of day</b></span>');
 $('runwindow').innerHTML=bits.join('');
}
async function runControl(which,btn){
 await busy(btn,which==='start'?'Starting...':'Stopping...',async()=>{
  try{
   const z=await api('/api/control/'+which,{method:'POST',body:'{}'});
   renderRun(z);
   say('runmsg',which==='start'?'Started. '+z.reason
     :'Stopping. The file already encoding finishes and is revealed; nothing new is claimed.');
  }catch(e){say('runmsg',e.message,true);}
 });
}

// Reports what the walk found, not just that it ran. An empty queue looks the
// same whether there was nothing to convert or the watched folders are wrong,
// and that is the one question this button exists to answer.
async function scanNow(btn){
 await busy(btn,'Walking every watched folder...',async()=>{
  try{
   const z=await api('/api/scan',{method:'POST',body:'{}'});
   if(!z.scanned){say('scanmsg',z.detail);return;}
   const bits=[z.queued?`Queued ${z.queued}.`:'Nothing new to convert.'];
   bits.push(`${z.eligible} eligible file${z.eligible===1?'':'s'} in the watched folders`);
   if(z.settling)bits.push(`${z.settling} still settling (size has not held still yet)`);
   if(z.skipped_visible)bits.push(`${z.skipped_visible} skipped for not being dot-hidden`);
   if((z.missing_roots||[]).length)bits.push('watched folder missing in this container: '+z.missing_roots.join(', '));
   say('scanmsg',bits.join(' - '),(z.missing_roots||[]).length>0);
   await refreshQueue();
  }catch(e){say('scanmsg',e.message,true);}
 });
}

// ---- status + history ----------------------------------------------------
async function refreshHealth(){
 // Authenticated, so one poll carries the run state, the window, the zone and
 // the local clock as well as the counts - the anonymous body has the counts
 // only, because a window and a filesystem map are configuration.
 const z=await api('/healthz');
 ADMIN=!!z.admin_configured;
 renderRun(z);renderAdmin();
 $('summary').innerHTML=`encoder <b style="color:${z.encoder==='libx264'?'var(--warn)':'var(--ok)'}">`+
  `${esc(z.encoder)}</b> · ${z.queued} queued · ${z.running} running · v${esc(z.version)}`+
  (z.encoder==='libx264'?` <code>(${esc(z.encoder_reason)})</code>`:'');
}
async function refreshJobs(){
 const st=$('statefilter').value;
 const j=await api('/api/jobs?limit=60'+(st?'&state='+st:''));
 $('jobtable').innerHTML='<tr><th>State</th><th>File</th><th>%</th><th>Size</th><th>Result</th><th></th></tr>'+
  (j.jobs.length?j.jobs.map(x=>{
   const saved=(x.src_bytes&&x.out_bytes)?Math.round((1-x.out_bytes/x.src_bytes)*100)+'% smaller':'';
   const stop=(x.state==='queued'||x.state==='running')
     ?`<button class="ghost" data-cancel="${esc(x.id)}">Cancel</button>`:'';
   return `<tr><td class="${x.state}">${esc(x.state)}${x.kind==='reveal'?' <span class="tag">reveal</span>':''}</td>`+
    `<td>${esc(x.path.split('/').pop())}</td><td>${x.progress??''}</td><td>${esc(saved)}</td>`+
    `<td><code>${esc(x.error||x.warning||x.rescan||x.output||'')}</code></td><td>${stop}</td></tr>`;
  }).join(''):'<tr><td colspan="6">Nothing here yet.</td></tr>');
}
async function cancelJob(id){
 try{await api('/api/jobs/'+id,{method:'DELETE'});await refreshQueue();await refreshJobs();}
 catch(e){alert(e.message);}
}

// ---- settings ------------------------------------------------------------
function field(spec){
 const v=SET[spec.key],src=SRC[spec.key];
 const tag=`<span class="tag ${src}">${src==='stored'?'saved here':src==='env'?'from container':'default'}</span>`;
 let input,extra='';
 if(spec.kind==='bool') input=`<input type="checkbox" data-key="${spec.key}" ${v?'checked':''}> ${esc(spec.label)}`;
 // A secret comes back masked, never in the clear. Posting the mask straight
 // back is what keeps it, so the box is pre-filled with the stars on purpose
 // and the help text has to say what emptying it does.
 else if(spec.secret){input=`<input type="password" data-key="${spec.key}" value="${esc(v)}" autocomplete="new-password">`;
  extra=v?' Saved. Leave the stars alone to keep it; empty the box to remove it.':'';}
 else if(spec.kind==='int') input=`<input type="number" data-key="${spec.key}" value="${esc(v)}">`;
 else if(spec.kind==='exts'||spec.kind==='patterns')
   input=`<textarea data-key="${spec.key}" placeholder="one per line">${esc((v||[]).join('\n'))}</textarea>`;
 else input=`<input type="text" data-key="${spec.key}" value="${esc(v)}">`;
 return `<label><span class="lab"><span>${spec.kind==='bool'?'':esc(spec.label)}</span>${tag}</span>`+
  `${input}<small>${esc(spec.help+extra)}</small></label>`;
}
function renderSettings(){
 // Every group a spec can carry has a box here. A setting whose group has none
 // renders nowhere at all, which is invisible until somebody asks why the new
 // option does nothing - tests/test_web_page.py fails instead.
 for(const [box,group] of [['locform','Locations'],['ruleform','Rules'],['genform','General'],
   ['schedform','Schedule'],['perfform','Performance'],['hookform','Notifications'],['secform','Security']]){
  $(box).innerHTML=SPECS.filter(s=>s.group===group&&s.key!=='watch_roots').map(field).join('');
 }
 renderWatch();
}
async function loadSettings(){
 const d=await api('/api/settings');
 SPECS=d.specs;SET=d.values;SRC=d.sources;renderSettings();
}
async function saveSettings(box,msg){
 const updates={};
 $(box).querySelectorAll('[data-key]').forEach(el=>{
  updates[el.dataset.key]=el.type==='checkbox'?el.checked:el.value;
 });
 // Deleting a profile confirms, revoking a key confirms, and this checkbox -
 // the only control here that can re-encode an entire library and then age the
 // originals out of the trash - used to ride along with a generic Save. Only on
 // the off->on transition: re-saving the form with it already on changes nothing.
 // The widening direction is turning this OFF: dot-hidden only is the narrow
 // mode, and leaving it makes every visible file in the watched folders
 // eligible at once. Only on the on->off transition, since re-saving a form
 // that was already off changes nothing.
 if(SET.hidden_only&&!updates.hidden_only){
  const d=SET.trash_keep_days;
  if(!confirm('Stop converting only dot-hidden files?\n\n'+
    'Every visible file in the watched folders becomes eligible, not just the dot-hidden ones. '+
    'Each one is re-encoded lossily and the original is replaced.\n\n'+
    `The originals go to trash and are deleted for good after ${d} day${d===1?'':'s'}.`)){
   // Dropped rather than sent as true, and the box is re-ticked by hand: a
   // failed save never re-renders the form, so relying on loadSettings() to
   // put it back would leave a box that is not what the daemon has.
   delete updates.hidden_only;
   const el=$(box).querySelector('[data-key="hidden_only"]');
   if(el) el.checked=!!SET.hidden_only;
  }
 }
 try{await api('/api/settings',{method:'PUT',body:JSON.stringify(updates)});await loadSettings();
  say(msg,'Saved. Takes effect on the next scan - no restart needed.');}
 catch(e){say(msg,e.message,true);}
}
function renderWatch(){
 const roots=SET.watch_roots||[];
 $('watchpills').innerHTML=roots.length?roots.map((p,i)=>
   `<span class="pill">${esc(p)}<button data-dropwatch="${i}" title="Remove">&times;</button></span>`).join('')
  :'<p class="hint">None set - every media root is watched.</p>';
}
async function putWatch(roots){
 try{await api('/api/settings',{method:'PUT',body:JSON.stringify({watch_roots:roots})});
  await loadSettings();say('locmsg','Saved.');}catch(e){say('locmsg',e.message,true);}
}
function addWatch(){const v=$('watchadd').value.trim();if(!v)return;
 $('watchadd').value='';putWatch([...(SET.watch_roots||[]),v]);}
function dropWatch(i){const r=[...(SET.watch_roots||[])];r.splice(i,1);putWatch(r);}

// ---- folder browser ------------------------------------------------------
let CURRENT='/';
function upOne(){return CURRENT.replace(/\/[^/]+\/?$/,'')||'/';}
async function browse(path){
 try{
  const d=await api('/api/fs?path='+encodeURIComponent(path||''));
  CURRENT=d.path;$('browsepath').textContent=d.path;
  $('browser').innerHTML=(d.entries.length?d.entries:[]).map(e=>
    `<div data-browse="${esc(e.path)}">${esc(e.name)}/</div>`).join('')
   +`<div data-addwatch="1" style="color:var(--link)">+ watch ${esc(d.path)}</div>`;
 }catch(e){say('locmsg',e.message,true);}
}
function addBrowsed(){putWatch([...new Set([...(SET.watch_roots||[]),CURRENT])]);}

// ---- hardware and encoders -----------------------------------------------
let HW=null;
async function loadHW(){
 HW=await api('/api/encoders');
 const cur=(HW.encoders||[]).find(e=>e.name===HW.in_use)||{};
 const act=(PROF.profiles||[]).find(p=>p.active);
 $('hwhero').innerHTML=
  `<div><div class="big">${esc(act?act.name:HW.in_use)}</div>
    <div class="sub">${act?esc(act.encoder)+' · quality '+act.quality+' · '+
      (act.max_height?act.max_height+'p':'source resolution'):'no profile'}</div></div>
   <div><div class="big">${esc((HW.gpu||'no GPU').split(',')[0])}</div>
    <div class="sub">${cur.hardware?'hardware encoding':'software encoding'} · ${esc(HW.why||'')}</div></div>`;

 // One line each, not a grid of cards: none of this is actionable. The reason a
 // failed encoder gives is the whole point of keeping it visible - "Cannot load
 // libnvcuvid" is how somebody learns Quick Sync wants /dev/dri passed through.
 $('hwsum').innerHTML=(HW.encoders||[]).map(e=>
  `<div class="hwline ${e.available?'':'off'}">
    <span class="dot ${e.available?'y':'n'}"></span><b>${esc(e.name)}</b>
    <span class="chip ${e.hardware?'gpu':'cpu'}">${e.hardware?'GPU':'CPU'}</span>
    <span class="chip">${esc(e.codec||'')}</span>
    ${e.name===HW.in_use?'<span class="using">in use</span>':''}
    <span class="why">${esc(e.available?e.summary:e.reason)}</span>
   </div>`).join('');
}
async function probeHW(btn){
 await busy(btn,'Testing every encoder...',async()=>{
  try{await api('/api/encoders/probe',{method:'POST'});await loadHW();say('hwmsg','Hardware re-tested.');}
  catch(e){say('hwmsg',e.message,true);}
 });
}
// ---- encoding profiles ---------------------------------------------------
let PROF={profiles:[],available_encoders:[]},editingProfile=null;
const opt=(v,l,sel)=>`<option value="${esc(v)}" ${String(v)===String(sel)?'selected':''}>${esc(l)}</option>`;
const encOf=name=>PROF.available_encoders.find(e=>e.name===name)||PROF.available_encoders[0]||null;
const profBy=id=>(PROF.profiles||[]).find(x=>x.id===id);

function profCard(p){
 const res=p.max_height?p.max_height+'p':'source';
 const aud=p.audio_codec==='copy'?'audio copied'
   :`AAC ${p.audio_bitrate}k ${p.audio_channels?p.audio_channels+'ch':'source ch'}`;
 // Three answers, never two: null is "nobody has looked", 0 is "this machine
 // cannot do it". Collapsing them into "never tested" made an untried profile
 // look as broken as one whose ffmpeg run actually failed.
 const state=p.validated_ok==null?'<span class="dim">not tested yet</span>'
   :p.validated_ok?'<span class="done">tested</span> '+esc(p.validated_note||'')
   :'<span class="failed">will not run on this machine</span> '+esc(p.validated_note||'');
 return `<div class="enc ${p.active?'on':''} ${p.usable?'':'off'}">
  <div class="top"><span class="nm">${esc(p.name)}</span>
   ${p.active?'<span class="using">default</span>':''}</div>
  <div class="chips"><span class="chip">${esc(p.encoder)}</span><span class="chip">q${p.quality}</span>
   <span class="chip">${esc(res)}</span>${p.preset?`<span class="chip">${esc(p.preset)}</span>`:''}
   ${p.profile?`<span class="chip">${esc(p.profile)}</span>`:''}</div>
  <div class="why">${esc(aud)}</div>
  <div class="why">${state}</div>
  <div class="row" style="margin-top:.4rem">
   ${p.active||!p.usable?'':`<button class="act" data-useprof="${esc(p.id)}">Use</button>`}
   <button class="ghost" data-dupprof="${esc(p.id)}">Duplicate</button>
   <button class="ghost" data-testprof="${esc(p.id)}">Test</button>
   ${p.builtin?'':`<button class="ghost" data-editprof="${esc(p.id)}">Edit</button>`}
   ${p.builtin||p.active?'':`<button class="ghost danger" data-delprof="${esc(p.id)}">Delete</button>`}
  </div></div>`;
}

async function loadProfiles(){
 PROF=await api('/api/profiles');
 // The server already orders these - shipped in encoder-probe order, then the
 // user's own oldest first - so this only splits, it never re-sorts.
 const all=PROF.profiles||[];
 $('profshipped').innerHTML=all.filter(p=>p.builtin).map(profCard).join('')
  ||'<p class="hint">None - this build shipped no profiles.</p>';
 $('profmine').innerHTML=all.filter(p=>!p.builtin).map(profCard).join('')
  ||'<p class="hint">None yet. Duplicate a shipped profile to start from one that works.</p>';

 // A named form is one somebody is part way through - a Duplicate they have
 // renamed, or a new profile half typed. Testing another card re-reads the list,
 // and that used to throw the typing away.
 if(!editingProfile&&!$('p_name').value) fillProfileForm(null);
}

function duplicateProfile(p){
 // The only way to customize a shipped profile, which cannot be edited. Clearing
 // editingProfile is the whole trick: Save then POSTs to /api/profiles and makes
 // a new row instead of writing back over the one being copied.
 editingProfile=null;
 // Copying a profile this machine cannot run lands on a different encoder, where
 // the same quality number is a different picture at a different size. Carry the
 // settings that travel and let the new encoder recommend the ones that do not.
 const {quality,preset,profile,...rest}=p;
 const same=PROF.available_encoders.some(e=>e.name===p.encoder);
 fillProfileForm({...(same?p:rest),name:'Copy of '+p.name});
 $('profedittitle').textContent='New profile';
 $('p_testout').style.display='none';
 $('p_name').scrollIntoView({behavior:'smooth',block:'center'});
 $('p_name').focus();
}

function fillProfileForm(p){
 const encs=PROF.available_encoders;
 if(!encs.length){$('p_enchelp').textContent='No working encoder found on this machine.';return;}
 const cur=p||{};
 $('p_name').value=cur.name||'';
 $('p_encoder').innerHTML=encs.map(e=>opt(e.name,`${e.name} (${e.codec}${e.hardware?', GPU':', CPU'})`,cur.encoder||encs[0].name)).join('');
 $('p_res').innerHTML=(PROF.resolutions||[]).map(r=>opt(r.value,r.label,cur.max_height??0)).join('');
 $('p_acodec').innerHTML=(PROF.audio_codecs||[]).map(a=>opt(a.value,a.label,cur.audio_codec||'aac')).join('');
 $('p_achan').innerHTML=(PROF.audio_channels||[]).map(a=>opt(a.value,a.label,cur.audio_channels??2)).join('');
 $('p_abitrate').value=cur.audio_bitrate??192;
 onEncoderChange(cur);
 onResChange();onAudioChange();
 $('profedittitle').textContent=p?('Edit '+p.name):'New profile';
}

function onEncoderChange(cur){
 const e=encOf($('p_encoder').value);
 if(!e) return;
 $('p_enchelp').textContent=e.summary||'';
 $('p_preset').innerHTML=(e.presets||[]).map(x=>opt(x.value,x.label,(cur&&cur.preset)||e.default_preset)).join('');
 $('p_profile').innerHTML=(e.profiles||[]).map(x=>opt(x.value,x.label,(cur&&cur.profile)||e.default_profile)).join('');
 const [lo,hi]=e.sane_range||[14,34];
 const q=$('p_quality');
 // The scale is per encoder: CQ 21 on NVENC and CRF 21 on x264 are different
 // pictures at different sizes, so the slider re-ranges rather than pretending
 // one number means one thing everywhere.
 q.min=Math.max(1,lo-4);q.max=hi+4;
 q.value=(cur&&cur.quality)||e.recommended_quality;
 $('p_qrec').textContent='suggested '+e.recommended_quality;
 onQualityChange();
}
function onQualityChange(){$('p_qval').textContent=$('p_quality').value;}
function onResChange(){
 const r=(PROF.resolutions||[]).find(x=>String(x.value)===$('p_res').value);
 $('p_reshelp').textContent=r?r.help:'';
}
function onAudioChange(){$('p_abr_wrap').style.display=$('p_acodec').value==='copy'?'none':'block';}

function profileBody(){
 return {name:$('p_name').value,encoder:$('p_encoder').value,quality:Number($('p_quality').value),
  preset:$('p_preset').value,profile:$('p_profile').value,max_height:Number($('p_res').value),
  audio_codec:$('p_acodec').value,audio_bitrate:Number($('p_abitrate').value),
  audio_channels:Number($('p_achan').value)};
}
function resetProfileForm(){editingProfile=null;$('p_testout').style.display='none';fillProfileForm(null);}

async function testProfile(btn){
 const box=$('p_testout');
 box.style.display='block';box.innerHTML='Encoding two seconds of test video with these exact settings...';
 await busy(btn,'Encoding a test clip...',async()=>{
  try{
   const r=await api('/api/profiles/test',{method:'POST',body:JSON.stringify(profileBody())});
   box.innerHTML=(r.ok?'<b class="done">Works.</b> ':'<b class="failed">Does not work.</b> ')+esc(r.detail)+
    `<br><code>${esc(r.command)}</code>`;
  }catch(e){box.innerHTML='<b class="failed">Test failed.</b> '+esc(e.message);}
 });
}
// The stored-profile test, not the form's dry run: the verdict is written to the
// row, so the list has to be re-read afterwards or the card keeps its old state.
async function testStoredProfile(id,btn){
 await busy(btn,'Testing...',async()=>{
  try{const r=await api('/api/profiles/'+id+'/test',{method:'POST',body:'{}'});
   await loadProfiles();
   say('hwmsg',(r.ok?'Works - ':'Will not run here - ')+r.detail,!r.ok);
  }catch(e){say('hwmsg',e.message,true);}
 });
}
async function retestProfiles(btn){
 await busy(btn,'Testing every profile, about a minute...',async()=>{
  try{await api('/api/profiles/retest',{method:'POST',body:'{}'});await loadProfiles();
   say('hwmsg','Every profile re-tested.');}
  catch(e){say('hwmsg',e.message,true);}
 });
}
async function saveProfile(btn){
 // Saving is not cheap either: the daemon refuses to store a profile it has not
 // just encoded with, so this button blocks for the same couple of seconds.
 await busy(btn,'Testing, then saving...',async()=>{
  try{
   const r=await api('/api/profiles'+(editingProfile?'/'+editingProfile:''),
     {method:'POST',body:JSON.stringify(profileBody())});
   resetProfileForm();await loadProfiles();await loadHW();
   say('hwmsg','Saved - '+r.detail);
  }catch(e){say('hwmsg',e.message,true);}
 });
}
async function useProfile(id){
 try{const r=await api('/api/profiles/'+id+'/activate',{method:'POST',body:'{}'});
  await loadProfiles();await loadHW();
  say('hwmsg',`Now using "${r.profile.name}". Jobs already running finish on the old one.`);
 }catch(e){say('hwmsg',e.message,true);}
}
async function deleteProfile(id,name){
 if(!confirm('Delete the profile "'+name+'"?'))return;
 try{await api('/api/profiles/'+id,{method:'DELETE'});await loadProfiles();say('hwmsg','Deleted.');}
 catch(e){say('hwmsg',e.message,true);}
}

// ---- arr connections -----------------------------------------------------
function arrFormHtml(a){
 a=a||{name:'',kind:'radarr',base_url:'',arr_path:'',worker_path:'',enabled:true};
 return `<label><span class="lab">Name</span><input type="text" id="a_name" value="${esc(a.name)}"></label>
 <label><span class="lab">Kind</span><select id="a_kind">
  <option value="radarr" ${a.kind==='radarr'?'selected':''}>Radarr (movies)</option>
  <option value="sonarr" ${a.kind==='sonarr'?'selected':''}>Sonarr (series)</option></select></label>
 <label><span class="lab">Base URL</span><input type="text" id="a_url" value="${esc(a.base_url)}"
  placeholder="http://radarr:7878"></label>
 <label><span class="lab">API key</span><input type="password" id="a_key" placeholder="${a.id?'unchanged':''}">
  <small>Settings &rarr; General &rarr; API Key. Stored in this container's database; leave blank when editing to keep it.</small></label>
 <label><span class="lab">Library path as the arr sees it</span><input type="text" id="a_arrpath"
  value="${esc(a.arr_path)}" placeholder="/tv"><small>Its root folder, e.g. /tv or /movies.</small></label>
 <label><span class="lab">The same folder as this container sees it</span><input type="text" id="a_workerpath"
  value="${esc(a.worker_path)}" placeholder="/media/TV"><small>Both mount the same directory; without this pair a
  finished file cannot be matched to a title.</small></label>
 <label><input type="checkbox" id="a_enabled" ${a.enabled?'checked':''}> Enabled</label>`;
}
function resetArrForm(){editingArr=null;$('arrformtitle').textContent='Add a connection';$('arrform').innerHTML=arrFormHtml(null);}
function arrBody(){return{name:$('a_name').value,kind:$('a_kind').value,base_url:$('a_url').value,
 api_key:$('a_key').value,arr_path:$('a_arrpath').value,worker_path:$('a_workerpath').value,
 enabled:$('a_enabled').checked};}
let ARRS=[];
async function loadArrs(){
 const d=await api('/api/arrs');
 ARRS=d.arrs;
 $('arrtable').innerHTML='<tr><th>Name</th><th>Kind</th><th>URL</th><th>Path mapping</th><th>Last result</th><th></th></tr>'+
  (d.arrs.length?d.arrs.map(a=>`<tr><td>${esc(a.name)}${a.enabled?'':' <span class="tag">off</span>'}</td>
   <td>${esc(a.kind)}</td><td><code>${esc(a.base_url)}</code></td>
   <td><code>${esc(a.arr_path||'?')} &rarr; ${esc(a.worker_path||'?')}</code></td>
   <td><code class="${a.last_error?'failed':''}">${esc(a.last_error||'ok')}</code></td>
   <td><button class="ghost" data-editarr="${esc(a.id)}">Edit</button>
    <button class="ghost danger" data-delarr="${esc(a.id)}">Delete</button></td></tr>`).join('')
   :'<tr><td colspan="6">No connections yet.</td></tr>');
}
function editArr(json){const a=typeof json==='string'?JSON.parse(json):json;editingArr=a.id;
 $('arrformtitle').textContent='Edit '+a.name;$('arrform').innerHTML=arrFormHtml(a);
 document.querySelector('nav button[data-tab=connections]').click();window.scrollTo(0,document.body.scrollHeight);}
async function saveArr(){
 try{await api('/api/arrs'+(editingArr?'/'+editingArr:''),
   {method:editingArr?'PUT':'POST',body:JSON.stringify(arrBody())});
  resetArrForm();await loadArrs();say('arrmsg','Saved.');}catch(e){say('arrmsg',e.message,true);}
}
async function testArr(){
 try{const r=await api('/api/arrs/test',{method:'POST',body:JSON.stringify({...arrBody(),id:editingArr})});
  say('arrmsg',r.ok?'Connected - '+r.detail:'Failed: '+r.detail,!r.ok);}catch(e){say('arrmsg',e.message,true);}
}
async function deleteArr(id,name){
 if(!confirm('Delete the connection "'+name+'"? Nothing on the arr itself is changed.'))return;
 try{await api('/api/arrs/'+id,{method:'DELETE'});await loadArrs();say('arrmsg','Deleted.');}
 catch(e){say('arrmsg',e.message,true);}
}

// ---- the admin account and its sessions ----------------------------------
// Text only, never innerHTML on the card: this runs on every poll and rebuilding
// the form would wipe a password somebody is half way through typing.
function renderAdmin(){
 $('admintitle').textContent=ADMIN?'Admin account':'Create an admin account';
 $('adminhint').textContent=ADMIN
  ?'Changing the username or the password costs the current password. The bootstrap token in the container '+
   'environment always gets back in, which is the way out of a forgotten one.'
  :'No password is set, so this box is protected by its API token alone. Create an account to get a login form.';
 $('ad_cur_wrap').style.display=ADMIN?'block':'none';
 $('firstrun').style.display=ADMIN?'none':'block';
}
async function saveAdmin(btn){
 await busy(btn,'Saving...',async()=>{
  try{
   const r=await api('/api/admin',{method:'POST',body:JSON.stringify({
     username:$('ad_user').value.trim(),password:$('ad_pass').value,current_password:$('ad_cur').value})});
   $('ad_pass').value='';$('ad_cur').value='';
   await refreshHealth();await loadSessions();
   say('keymsg','Saved. Sign in as "'+r.username+'" from now on. Sessions already open stay open - revoke them '+
     'below if the old password is the reason you changed it.');
  }catch(e){say('keymsg',e.message,true);}
 });
}
let SESSIONS=[];
async function loadSessions(){
 const d=await api('/api/sessions');
 SESSIONS=d.sessions;
 if(!$('ad_user').value) $('ad_user').value=d.admin||'';
 // The prefix is the first 11 characters of the token this browser is holding,
 // which is how the page can point at its own row without ever sending it back.
 const mine=(localStorage.token||'').slice(0,11);
 $('sesstable').innerHTML='<tr><th>Session</th><th>Signed in</th><th>Last used</th><th>Expires</th><th></th></tr>'+
  (d.sessions.length?d.sessions.map(s=>`<tr><td><code>${esc(s.prefix)}...</code>`+
   `${s.prefix===mine?' <span class="tag stored">this browser</span>':''}</td>
   <td>${new Date(s.created*1000).toLocaleString()}</td>
   <td>${s.last_used?new Date(s.last_used*1000).toLocaleString():'never'}</td>
   <td>${new Date(s.expires*1000).toLocaleDateString()}</td>
   <td><button class="ghost danger" data-revokesession="${esc(s.id)}">Revoke</button></td></tr>`).join('')
   :'<tr><td colspan="5">Nobody is signed in with a password right now.</td></tr>');
}
async function revokeSession(id){
 const s=SESSIONS.find(x=>x.id===id),own=s&&s.prefix===(localStorage.token||'').slice(0,11);
 if(!confirm(own?'Revoke this browser\'s own login? You are signed out immediately.'
   :'Revoke this login? That browser is signed out on its next request.')) return;
 try{await api('/api/sessions/'+id,{method:'DELETE'});await loadSessions();say('keymsg','Revoked.');}
 catch(e){say('keymsg',e.message,true);}
}

// ---- backup, restore, and a certificate ----------------------------------
async function downloadBackup(btn){
 await busy(btn,'Preparing...',async()=>{
  try{
   // Fetched and handed over as a blob rather than linked: a plain link cannot
   // carry the bearer header, and a token in a query string is a token in every
   // proxy log between here and the browser.
   const r=await fetch('/api/backup',{headers:{Authorization:'Bearer '+token()}});
   if(!r.ok) throw new Error('HTTP '+r.status);
   const name=(r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/);
   const a=document.createElement('a');
   a.href=URL.createObjectURL(new Blob([await r.text()],{type:'application/json'}));
   a.download=name?name[1]:'transcodearr-config.json';
   document.body.appendChild(a);a.click();a.remove();
   setTimeout(()=>URL.revokeObjectURL(a.href),10000);
   say('sysmsg','Downloaded.');
  }catch(e){say('sysmsg',e.message,true);}
 });
}
async function restoreBackup(btn){
 const f=($('restorefile').files||[])[0];
 if(!f) return say('sysmsg','Choose a backup file first.',true);
 let doc;
 try{doc=JSON.parse(await f.text());}catch(e){return say('sysmsg','That file is not JSON.',true);}
 // Not a generic Save. This rewrites the settings and profiles every future job
 // runs with, usually onto a box that already has work queued, so it asks in
 // those words rather than "are you sure".
 if(!confirm('Restore this configuration?\n\n'+
   'It overwrites the settings, the profiles and the arr connections on this box, and every job from now on '+
   'runs with what is in this file. Files already converted are untouched.\n\n'+
   'No arr API key is ever in a backup, so each connection needs its key typed in again.\n\n'+
   'Written by TranscodeArr '+(doc.version||'?')+'.')) return;
 await busy(btn,'Restoring...',async()=>{
  try{
   const r=await api('/api/restore',{method:'POST',body:JSON.stringify(doc)});
   $('restoreout').style.display='block';
   // Shown line by line and verbatim: the list names what the restore skipped
   // on purpose, and that a restored profile comes back untested and cannot be
   // made the default until a real encode passes here.
   $('restoreout').innerHTML='<b>Restored.</b><br>'+
    (r.changed.length?r.changed.map(esc).join('<br>'):'Nothing differed from what was already here.');
   await loadSettings();await loadArrs();await loadProfiles();
  }catch(e){say('sysmsg',e.message,true);}
 });
}
async function selfSign(btn){
 const host=$('tlshost').value.trim();
 if(!host) return say('sysmsg','Type the hostname or IP address you open this page on.',true);
 await busy(btn,'Generating...',async()=>{
  try{
   const r=await api('/api/tls/selfsigned',{method:'POST',body:JSON.stringify({host})});
   // Filled in above but deliberately not saved: serving HTTPS needs a restart,
   // and a bad pair stops the container, so pressing Save stays a decision.
   const cert=$('secform').querySelector('[data-key="tls_cert"]'),
         key=$('secform').querySelector('[data-key="tls_key"]');
   if(cert) cert.value=r.cert;
   if(key) key.value=r.key;
   $('tlsout').style.display='block';
   $('tlsout').innerHTML='<b>Created.</b> The two paths are filled in above - press Save, then restart the '+
    'container.<br><code>'+esc(r.cert)+'</code><br><code>'+esc(r.key)+'</code><br>'+esc(r.detail);
  }catch(e){say('sysmsg',e.message,true);}
 });
}

// ---- API keys ------------------------------------------------------------
async function loadKeys(){
 const d=await api('/api/tokens');
 $('keytable').innerHTML='<tr><th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th></th></tr>'+
  (d.tokens.length?d.tokens.map(t=>`<tr><td>${esc(t.name)}</td><td><code>${esc(t.prefix)}...</code></td>
   <td>${new Date(t.created*1000).toLocaleDateString()}</td>
   <td>${t.last_used?new Date(t.last_used*1000).toLocaleString():'never'}</td>
   <td><button class="ghost danger" data-revoke="${esc(t.id)}">Revoke</button></td></tr>`).join('')
   :'<tr><td colspan="5">No keys minted. The container environment token is in use.</td></tr>');
 KEYS=d.tokens;
}
let KEYS=[];
async function mintKey(){
 const name=$('keyname').value.trim()||'unnamed';
 try{const r=await api('/api/tokens',{method:'POST',body:JSON.stringify({name})});
  $('keyname').value='';
  $('keyout').style.display='block';
  $('keyout').innerHTML=`<b>${esc(name)}</b> created. Copy it now - it is not shown again:<br>
   <code>${esc(r.token)}</code>`;
  await loadKeys();}catch(e){say('keymsg',e.message,true);}
}
async function revokeKey(id,name){
 if(!confirm('Revoke "'+name+'"? Anything using it stops working immediately.'))return;
 try{await api('/api/tokens/'+id,{method:'DELETE'});await loadKeys();say('keymsg','Revoked.');}
 catch(e){say('keymsg',e.message,true);}
}

// ---- boot ----------------------------------------------------------------
async function tick(){
 try{
  await refreshHealth();
  // Only refresh the visible tab: polling the history and the queue together
  // every few seconds is two queries nobody is looking at.
  if($('queue').classList.contains('on')){await refreshQueue();await refreshHost();}
  else if($('jobs').classList.contains('on')) await refreshJobs();
 }catch(e){}
}
async function boot(){
 resetArrForm();
 try{await refreshHealth();await loadSettings();await loadArrs();await loadKeys();await loadSessions();
  await loadProfiles();await loadHW();
  await refreshQueue();await refreshHost();await refreshJobs();
  browse((SET.watch_roots||[])[0]||'');
  if(timer) clearInterval(timer);
  timer=setInterval(tick,4000);
 }catch(e){if(String(e.message)!=='unauthorized')$('summary').textContent=e.message;}
}
// No credential means the sign-in screen, not a blank page: showGate asks the
// unauthenticated /healthz whether this box has a password and draws the form
// that can actually get in.
if(localStorage.token) boot(); else showGate('');
$('statefilter').onchange=refreshJobs;
// The same attribute the pre-paint line at the top of the document writes, so a
// switch and a reload land on the same rule. "system" is stored as itself and
// resolved by the media query, never by this - resolving it here would mean a
// second value to keep in step every time the OS changed underneath the tab.
function setTheme(v){localStorage.theme=v;document.documentElement.dataset.theme=v;}
$('theme').value=localStorage.theme||'dark';
// Enter submits, because a login form that ignores it reads as broken.
$('g_pass').onkeydown=e=>{if(e.key==='Enter')signIn($('g_signin'));};
$('g_token').onkeydown=e=>{if(e.key==='Enter')useToken($('g_usetoken'));};
</script>"""

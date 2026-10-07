#!/usr/bin/env python3
"""Render the guides/*.md ramp-up library into styled, self-contained HTML.

Usage:  python3 build.py
Deps:   pip install markdown pygments
Output: html/*.html  and  index.html
"""

from __future__ import annotations

import html as htmllib
import json
import os
import re
import shutil
import sys

try:
    import markdown
    from pygments.formatters import HtmlFormatter
except ImportError:  # pragma: no cover
    sys.exit("Missing deps. Run: pip install markdown pygments")

ROOT = os.path.dirname(os.path.abspath(__file__))
GUIDES = os.path.join(ROOT, "guides")
OUT = os.path.join(ROOT, "html")

LIBRARY_TITLE = "Temporal Cloud Infrastructure Ramp-Up"
LIBRARY_SUB = "New-role ramp-up &middot; public sources only"

DISCLAIMER_HTML = "<strong>Disclaimer.</strong> I wrote this library before I joined Temporal, to ramp up on the areas my new role would likely touch. It contains no internal information &mdash; nothing here was sourced from Temporal&rsquo;s internal knowledge. Temporal is open source, so the programming model and server internals come from public code and documentation, and every statement about Temporal Cloud comes from Temporal&rsquo;s public docs and engineering blog, linked inline. Most of the library is general infrastructure technology &mdash; Go, gRPC, Kubernetes, Karpenter, Terraform, Helm, Vault, cert-manager, GitOps, observability &mdash; that has nothing to do with any one company. Where a guide goes beyond public sources, it says so and labels the claim as inference."

# Short labels + tags for the sidebar and index cards.
META = {
    "01-golang": ("Go", ["language", "controllers", "runtime"]),
    "02-grpc": ("gRPC &amp; Protobuf", ["rpc", "http/2", "load balancing"]),
    "03-multicloud-aws-gcp-azure": ("Multi-Cloud", ["aws", "gcp", "azure", "iam"]),
    "04-managed-kubernetes-eks-gke-aks": (
        "Managed Kubernetes",
        ["eks", "gke", "aks", "upgrades"],
    ),
    "05-cni-and-host-networking": (
        "CNI &amp; Host Networking",
        ["ebpf", "kube-proxy", "dns", "debugging"],
    ),
    "06-karpenter": ("Karpenter", ["autoscaling", "nodes", "spot"]),
    "07-kyverno": ("Kyverno", ["policy", "admission", "supply chain"]),
    "08-terraform": ("Terraform", ["iac", "state", "modules"]),
    "09-helm": ("Helm", ["templating", "gitops", "rendered manifests"]),
    "10-vault": ("Vault", ["secrets", "auth", "dynamic creds"]),
    "11-cert-manager-and-pki": ("cert-manager &amp; PKI", ["tls", "ca", "rotation"]),
    "12-cell-lifecycle-synthesis": (
        "Cell Lifecycle",
        ["synthesis", "bootstrap", "90 days"],
    ),
    "13-gitops-argocd-flux": (
        "GitOps",
        ["argo cd", "flux", "applicationsets", "pruning"],
    ),
    "14-observability-for-cells": (
        "Observability",
        ["prometheus", "otel", "slo", "cardinality"],
    ),
    "15-temporal-programming-model": (
        "Temporal: Programming Model",
        ["workflows", "determinism", "workers"],
    ),
    "16-temporal-server-internals": (
        "Temporal Server Internals",
        ["shards", "matching", "persistence", "source"],
    ),
    "17-cell-based-architecture": (
        "Cell-Based Architecture",
        ["cells", "routing", "placement", "migration"],
    ),
}

READING_WPM = 200


# --------------------------------------------------------------------------- css


def pygments_css() -> str:
    light = HtmlFormatter(style="friendly").get_style_defs(".codehilite")
    for dark_style in ("github-dark", "monokai", "native"):
        try:
            fmt = HtmlFormatter(style=dark_style)
            break
        except Exception:  # pragma: no cover
            continue
    dark_attr = fmt.get_style_defs('html[data-theme="dark"] .codehilite')
    dark_media = fmt.get_style_defs('html:not([data-theme="light"]) .codehilite')
    return (
        light
        + "\n"
        + dark_attr
        + "\n@media (prefers-color-scheme: dark){\n"
        + dark_media
        + "\n}\n"
    )


BASE_CSS = r"""
:root{
  --bg:#fbfbfd; --bg-elev:#ffffff; --bg-sunk:#f2f3f7;
  --fg:#1a1c22; --fg-mid:#4b5060; --fg-dim:#7b8194;
  --line:#e3e5ec; --line-soft:#eef0f5;
  --accent:#4f46e5; --accent-soft:#eef0ff; --accent-fg:#3730a3;
  --warn:#b45309; --warn-soft:#fff7ed;
  --ok:#0f766e; --ok-soft:#effaf7;
  --code-bg:#f6f7fa;
  --sidebar-w:270px; --rail-w:220px;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
}
@media (prefers-color-scheme:dark){
  html:not([data-theme="light"]){
    --bg:#0f1117; --bg-elev:#151821; --bg-sunk:#1b1f2a;
    --fg:#e6e8ef; --fg-mid:#a8aec0; --fg-dim:#767d92;
    --line:#252a36; --line-soft:#1e222d;
    --accent:#8b87f5; --accent-soft:#1e1f38; --accent-fg:#b3b0ff;
    --warn:#fbbf24; --warn-soft:#2a2113;
    --ok:#5eead4; --ok-soft:#10241f;
    --code-bg:#12151d;
  }
}
html[data-theme="dark"]{
  --bg:#0f1117; --bg-elev:#151821; --bg-sunk:#1b1f2a;
  --fg:#e6e8ef; --fg-mid:#a8aec0; --fg-dim:#767d92;
  --line:#252a36; --line-soft:#1e222d;
  --accent:#8b87f5; --accent-soft:#1e1f38; --accent-fg:#b3b0ff;
  --warn:#fbbf24; --warn-soft:#2a2113;
  --ok:#5eead4; --ok-soft:#10241f;
  --code-bg:#12151d;
}

*{box-sizing:border-box}
html{scroll-behavior:smooth; scroll-padding-top:1.5rem}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font-family:var(--sans); font-size:16px; line-height:1.68;
  -webkit-font-smoothing:antialiased;
}
a{color:var(--accent); text-decoration:none}
a:hover{text-decoration:underline; text-underline-offset:2px}

/* ---------- shell ---------- */
.shell{display:flex; min-height:100vh}
.sidebar{
  width:var(--sidebar-w); flex:0 0 var(--sidebar-w);
  border-right:1px solid var(--line); background:var(--bg-elev);
  position:sticky; top:0; height:100vh; overflow-y:auto; padding:1.4rem 0 3rem;
}
.brand{padding:0 1.25rem 1.1rem; border-bottom:1px solid var(--line-soft); margin-bottom:.9rem}
.brand a{color:var(--fg); display:block}
.brand a:hover{text-decoration:none}
.brand-title{font-weight:650; font-size:.95rem; letter-spacing:-.01em}
.disclaimer{margin-top:3rem; padding-top:1rem; border-top:1px solid var(--line, rgba(127,127,127,.25)); font-size:.8rem; line-height:1.55; color:var(--fg-dim)}
.brand-sub{font-size:.75rem; color:var(--fg-dim); margin-top:.15rem}
.nav-label{
  padding:.2rem 1.25rem .45rem; font-size:.68rem; letter-spacing:.09em;
  text-transform:uppercase; color:var(--fg-dim); font-weight:600;
}
.nav a{
  display:flex; gap:.6rem; align-items:baseline;
  padding:.36rem 1.25rem; color:var(--fg-mid); font-size:.855rem; line-height:1.4;
  border-left:2px solid transparent;
}
.nav a:hover{background:var(--bg-sunk); color:var(--fg); text-decoration:none}
.nav a.active{
  color:var(--accent-fg); background:var(--accent-soft);
  border-left-color:var(--accent); font-weight:600;
}
.nav .num{font-family:var(--mono); font-size:.72rem; color:var(--fg-dim); min-width:1.3em}
.nav a.active .num{color:var(--accent)}

/* ---------- main ---------- */
.main{flex:1; min-width:0; display:flex; justify-content:center; gap:2rem; padding:0 2rem}
.doc{max-width:60rem; width:100%; padding:2.6rem 0 6rem; min-width:0}
.rail{
  width:var(--rail-w); flex:0 0 var(--rail-w); position:sticky; top:0;
  height:100vh; overflow-y:auto; padding:2.9rem 0 4rem; font-size:.79rem;
}
.rail-label{
  font-size:.68rem; letter-spacing:.09em; text-transform:uppercase;
  color:var(--fg-dim); font-weight:600; margin-bottom:.55rem;
}
.rail a{display:block; padding:.2rem 0 .2rem .7rem; color:var(--fg-dim);
  border-left:2px solid var(--line); line-height:1.45}
.rail a:hover{color:var(--fg); text-decoration:none; border-left-color:var(--fg-dim)}
.rail a.h3{padding-left:1.5rem; font-size:.75rem}
.rail a.here{color:var(--accent-fg); border-left-color:var(--accent); font-weight:600}

/* ---------- topbar ---------- */
.topbar{
  display:flex; align-items:center; justify-content:space-between; gap:1rem;
  padding-bottom:1.4rem; margin-bottom:1.6rem; border-bottom:1px solid var(--line);
  font-size:.78rem; color:var(--fg-dim);
}
.crumbs{display:flex; gap:.5rem; align-items:center; flex-wrap:wrap}
.pill{
  display:inline-block; padding:.14rem .5rem; border-radius:999px;
  background:var(--bg-sunk); color:var(--fg-mid); font-size:.7rem;
  border:1px solid var(--line-soft); white-space:nowrap;
}
.iconbtn{
  background:var(--bg-elev); border:1px solid var(--line); color:var(--fg-mid);
  border-radius:7px; padding:.3rem .6rem; font-size:.75rem; cursor:pointer;
  font-family:var(--sans);
}
.iconbtn:hover{background:var(--bg-sunk); color:var(--fg)}

/* ---------- prose ---------- */
.doc h1{font-size:2.05rem; line-height:1.2; letter-spacing:-.022em; margin:.2rem 0 1rem; font-weight:700}
.doc h2{
  font-size:1.42rem; letter-spacing:-.015em; margin:3.2rem 0 1rem; font-weight:660;
  padding-top:1.2rem; border-top:1px solid var(--line-soft);
}
.doc h3{font-size:1.09rem; margin:2.1rem 0 .7rem; font-weight:650; letter-spacing:-.008em}
.doc h4{font-size:.95rem; margin:1.5rem 0 .5rem; font-weight:640; color:var(--fg-mid)}
.doc h2:first-of-type{border-top:0; padding-top:0}
.doc p{margin:0 0 1.05rem}
.doc ul,.doc ol{margin:0 0 1.15rem; padding-left:1.45rem}
.doc li{margin:.32rem 0}
.doc li>ul,.doc li>ol{margin:.32rem 0}
.doc strong{font-weight:645; color:var(--fg)}
.doc hr{border:0; border-top:1px solid var(--line); margin:2.4rem 0}

.anchor{
  opacity:0; margin-left:.4rem; font-weight:400; color:var(--fg-dim);
  font-size:.75em; text-decoration:none;
}
h2:hover .anchor,h3:hover .anchor{opacity:1}

blockquote{
  margin:1.3rem 0; padding:.85rem 1.1rem; border-left:3px solid var(--accent);
  background:var(--accent-soft); border-radius:0 8px 8px 0; color:var(--fg-mid);
}
blockquote p:last-child{margin-bottom:0}

/* lede: the "Why this matters" opener */
.lede{
  background:var(--bg-elev); border:1px solid var(--line);
  border-left:3px solid var(--accent);
  padding:1rem 1.2rem; border-radius:0 10px 10px 0; margin:0 0 2rem;
  color:var(--fg-mid); font-size:.95rem;
}
.lede strong:first-child{color:var(--accent-fg)}
.lede p:last-child{margin-bottom:0}

/* ---------- code ---------- */
code{
  font-family:var(--mono); font-size:.855em; background:var(--code-bg);
  padding:.12em .38em; border-radius:5px; border:1px solid var(--line-soft);
  word-break:break-word;
}
.codewrap{position:relative; margin:0 0 1.35rem}
.codehilite{
  background:var(--code-bg); border:1px solid var(--line);
  border-radius:10px; overflow-x:auto; margin:0;
}
.codehilite pre{margin:0; padding:.95rem 1.05rem; font-size:.82rem; line-height:1.6}
.codehilite code{background:none; border:0; padding:0; font-size:inherit}
.copybtn{
  position:absolute; top:.5rem; right:.5rem; opacity:0; transition:opacity .12s;
  background:var(--bg-elev); border:1px solid var(--line); color:var(--fg-dim);
  border-radius:6px; padding:.16rem .5rem; font-size:.68rem; cursor:pointer;
  font-family:var(--sans);
}
.codewrap:hover .copybtn{opacity:1}
.copybtn:hover{color:var(--fg); background:var(--bg-sunk)}

/* ---------- tables ---------- */
.tablewrap{overflow-x:auto; margin:0 0 1.4rem; border:1px solid var(--line); border-radius:10px}
table{border-collapse:collapse; width:100%; font-size:.845rem; background:var(--bg-elev)}
th,td{padding:.55rem .8rem; text-align:left; border-bottom:1px solid var(--line-soft); vertical-align:top}
th{background:var(--bg-sunk); font-weight:640; font-size:.76rem;
  letter-spacing:.02em; text-transform:uppercase; color:var(--fg-mid); white-space:nowrap}
tr:last-child td{border-bottom:0}
td code{white-space:nowrap}

/* ---------- references ---------- */
#references + ol, .refs{font-size:.855rem; color:var(--fg-mid)}
#references + ol li{margin:.42rem 0; padding-left:.2rem}
#references + ol li a{font-weight:560}

/* ---------- index page ---------- */
.hero{max-width:60rem; margin:0 auto; padding:3.4rem 0 1.6rem}
.hero h1{font-size:2.5rem; letter-spacing:-.028em; margin:0 0 .6rem; font-weight:720; line-height:1.12}
.hero .tagline{font-size:1.06rem; color:var(--fg-mid); margin:0 0 1.6rem; max-width:44rem}
.stats{display:flex; gap:2rem; flex-wrap:wrap; padding:1.1rem 0; border-top:1px solid var(--line);
  border-bottom:1px solid var(--line); margin-bottom:2.4rem}
.stat .n{font-size:1.5rem; font-weight:680; letter-spacing:-.02em; display:block; line-height:1.1}
.stat .l{font-size:.72rem; color:var(--fg-dim); text-transform:uppercase; letter-spacing:.07em}
.cards{display:grid; grid-template-columns:repeat(auto-fill,minmax(19rem,1fr)); gap:1rem; margin-bottom:3rem}
.card{
  display:block; background:var(--bg-elev); border:1px solid var(--line);
  border-radius:12px; padding:1.15rem 1.2rem; color:var(--fg);
  transition:border-color .12s, transform .12s;
}
.card:hover{text-decoration:none; border-color:var(--accent); transform:translateY(-1px)}
.card .cnum{font-family:var(--mono); font-size:.7rem; color:var(--accent); font-weight:600}
.card h3{margin:.3rem 0 .5rem; font-size:1.04rem; font-weight:650; letter-spacing:-.01em; line-height:1.3}
.card p{margin:0 0 .8rem; font-size:.83rem; color:var(--fg-mid); line-height:1.55}
.tags{display:flex; gap:.32rem; flex-wrap:wrap}
.tag{font-size:.66rem; padding:.1rem .45rem; border-radius:5px; background:var(--bg-sunk);
  color:var(--fg-dim); border:1px solid var(--line-soft); white-space:nowrap}
.card-foot{margin-top:.7rem; font-size:.71rem; color:var(--fg-dim); font-family:var(--mono)}
.searchbox{
  width:100%; padding:.7rem .9rem; font-size:.9rem; font-family:var(--sans);
  background:var(--bg-elev); border:1px solid var(--line); border-radius:10px;
  color:var(--fg); margin-bottom:1.4rem;
}
.searchbox:focus{outline:none; border-color:var(--accent)}
.hit{padding:.4rem .2rem; border-bottom:1px solid var(--line-soft); font-size:.85rem}
.hit .where{font-size:.72rem; color:var(--fg-dim); margin-left:.4rem}

.note{
  background:var(--warn-soft); border:1px solid var(--line); border-left:3px solid var(--warn);
  border-radius:0 10px 10px 0; padding:.9rem 1.1rem; font-size:.86rem;
  color:var(--fg-mid); margin:0 0 2rem;
}
.note p:last-child{margin-bottom:0}

/* ---------- responsive ---------- */
@media (max-width:1180px){ .rail{display:none} .main{padding:0 1.6rem} }
@media (max-width:860px){
  .shell{flex-direction:column}
  .sidebar{width:100%; flex:none; height:auto; position:static; border-right:0;
    border-bottom:1px solid var(--line); padding-bottom:1rem}
  .nav{display:flex; flex-wrap:wrap; gap:.1rem}
  .nav a{border-left:0; border-radius:7px; padding:.3rem .6rem; margin:0 .3rem}
  .nav a.active{border-left:0}
  .main{padding:0 1.15rem}
  .doc{padding:1.8rem 0 4rem}
  .doc h1{font-size:1.68rem}
  .doc h2{font-size:1.24rem}
  .hero{padding:2rem 0 1rem}
  .hero h1{font-size:1.85rem}
  body{font-size:15.5px}
}

/* ---------- print ---------- */
@media print{
  .sidebar,.rail,.topbar,.copybtn,.searchbox{display:none !important}
  .main{padding:0} .doc{max-width:none; padding:0}
  body{font-size:10.5pt; background:#fff; color:#000}
  .codehilite{border:1px solid #ccc} a{color:#000; text-decoration:underline}
  .doc h2{page-break-after:avoid} .codewrap,.tablewrap{page-break-inside:avoid}
}
"""

THEME_JS = r"""
(function(){
  try{
    var t=localStorage.getItem('rampup-theme');
    if(t){document.documentElement.setAttribute('data-theme',t);}
  }catch(e){}
  window.__toggleTheme=function(){
    var el=document.documentElement;
    var cur=el.getAttribute('data-theme');
    if(!cur){
      cur=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
    }
    var next=cur==='dark'?'light':'dark';
    el.setAttribute('data-theme',next);
    try{localStorage.setItem('rampup-theme',next);}catch(e){}
  };
})();
"""

PAGE_JS = r"""
document.querySelectorAll('.codehilite').forEach(function(block){
  var wrap=document.createElement('div'); wrap.className='codewrap';
  block.parentNode.insertBefore(wrap,block); wrap.appendChild(block);
  var b=document.createElement('button'); b.className='copybtn'; b.textContent='copy';
  b.addEventListener('click',function(){
    var txt=block.innerText;
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(txt).then(function(){
        b.textContent='copied'; setTimeout(function(){b.textContent='copy';},1200);
      }).catch(function(){b.textContent='select manually';});
    } else {
      var ta=document.createElement('textarea'); ta.value=txt; document.body.appendChild(ta);
      ta.select(); try{document.execCommand('copy'); b.textContent='copied';}catch(e){}
      document.body.removeChild(ta); setTimeout(function(){b.textContent='copy';},1200);
    }
  });
  wrap.appendChild(b);
});

document.querySelectorAll('.doc table').forEach(function(t){
  if(t.parentNode.classList.contains('tablewrap'))return;
  var w=document.createElement('div'); w.className='tablewrap';
  t.parentNode.insertBefore(w,t); w.appendChild(t);
});

(function(){
  var links=Array.prototype.slice.call(document.querySelectorAll('.rail a'));
  if(!links.length)return;
  var map={};
  links.forEach(function(a){
    var el=document.getElementById(a.getAttribute('href').slice(1));
    if(el)map[a.getAttribute('href').slice(1)]=a;
  });
  var obs=new IntersectionObserver(function(entries){
    entries.forEach(function(e){
      var a=map[e.target.id]; if(!a)return;
      if(e.isIntersecting){
        links.forEach(function(x){x.classList.remove('here');});
        a.classList.add('here');
      }
    });
  },{rootMargin:'0px 0px -78% 0px',threshold:0});
  Object.keys(map).forEach(function(id){
    var el=document.getElementById(id); if(el)obs.observe(el);
  });
})();
"""


# --------------------------------------------------------------------------- helpers


def slug_of(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def guide_files() -> list[str]:
    return sorted(
        os.path.join(GUIDES, f) for f in os.listdir(GUIDES) if f.endswith(".md")
    )


def sidebar_html(files: list[str], active: str | None, prefix: str) -> str:
    rows = []
    for f in files:
        s = slug_of(f)
        label = META.get(s, (s, []))[0]
        num = s.split("-")[0]
        cls = ' class="active"' if s == active else ""
        rows.append(
            f'<a href="{prefix}{s}.html"{cls}><span class="num">{num}</span>'
            f"<span>{label}</span></a>"
        )
    home = "index.html" if prefix else "../index.html"
    drill = "drill.html" if prefix else "../drill.html"
    return f"""<aside class="sidebar">
  <div class="brand"><a href="{home}">
    <div class="brand-title">{LIBRARY_TITLE}</div>
    <div class="brand-sub">{LIBRARY_SUB}</div>
  </a></div>
  <div class="nav-label">Guides</div>
  <nav class="nav">{"".join(rows)}</nav>
  <div class="nav-label" style="margin-top:1rem">Practice</div>
  <nav class="nav"><a href="{drill}"><span class="num">&#9670;</span><span>Drill deck</span></a></nav>
</aside>"""


def rail_html(toc_tokens: list) -> str:
    out = []
    for t in toc_tokens:
        out.append(f'<a href="#{t["id"]}">{htmllib.escape(strip_tags(t["name"]))}</a>')
        for c in t.get("children", []):
            out.append(
                f'<a class="h3" href="#{c["id"]}">{htmllib.escape(strip_tags(c["name"]))}</a>'
            )
    if not out:
        return ""
    return (
        '<aside class="rail"><div class="rail-label">On this page</div>'
        + "".join(out)
        + "</aside>"
    )


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def add_anchors(body: str) -> str:
    def repl(m):
        tag, hid, inner = m.group(1), m.group(2), m.group(3)
        return f'<{tag} id="{hid}">{inner}<a class="anchor" href="#{hid}">#</a></{tag}>'

    return re.sub(r'<(h[23]) id="([^"]+)">(.*?)</\1>', repl, body, flags=re.S)


def wrap_lede(body: str) -> str:
    """Style the opening 'Why this matters' paragraph."""
    m = re.search(r"<p><strong>Why this matters[^<]*</strong>.*?</p>", body, flags=re.S)
    if not m:
        return body
    return body[: m.start()] + f'<div class="lede">{m.group(0)}</div>' + body[m.end() :]


def gh_slugify(value: str, separator: str = "-") -> str:
    """GitHub-compatible heading slugs.

    The in-guide cross-links were written GitHub-style, so the generated anchors
    must match GitHub's rules exactly: strip tags, lowercase, drop punctuation,
    then replace each remaining space with a separator WITHOUT collapsing runs.
    Python-Markdown's default slugify collapses runs, which silently breaks every
    link into a heading containing an em dash or a colon.
    """
    value = re.sub(r"<[^>]+>", "", value)
    value = htmllib.unescape(value)
    value = value.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value)
    return value.replace(" ", separator)


def make_md():
    return markdown.Markdown(
        extensions=[
            "fenced_code",
            "tables",
            "toc",
            "codehilite",
            "attr_list",
            "sane_lists",
        ],
        extension_configs={
            "codehilite": {"guess_lang": False, "css_class": "codehilite"},
            "toc": {"toc_depth": "2-3", "slugify": gh_slugify},
        },
    )


def page(title: str, head_extra: str, body: str, css_href: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{css_href}">
<script>{THEME_JS}</script>
{head_extra}
</head>
<body>
{body}
<script>/* open external links in a new tab; links within the library stay in this tab */
document.addEventListener('click',function(e){{var a=e.target.closest&&e.target.closest('a[href]');if(!a)return;var h=a.getAttribute('href');if(!h||h.charAt(0)==='#'||h.indexOf('javascript:')===0||a.hasAttribute('download'))return;if(!/^https?:$/.test(a.protocol)||a.host===location.host)return;a.target='_blank';a.rel='noopener';}},true);
</script>
</body>
</html>
"""


def topbar(right_label: str, pills: list[str]) -> str:
    pill_html = "".join(f'<span class="pill">{p}</span>' for p in pills)
    return f"""<div class="topbar">
  <div class="crumbs">{pill_html}</div>
  <div style="display:flex;gap:.5rem;align-items:center">
    <span>{right_label}</span>
    <button class="iconbtn" onclick="__toggleTheme()">theme</button>
  </div>
</div>"""


# --------------------------------------------------------------------------- build


def build():
    os.makedirs(OUT, exist_ok=True)
    files = guide_files()
    if not files:
        sys.exit(f"No .md files found in {GUIDES}")

    with open(os.path.join(OUT, "style.css"), "w") as fh:
        fh.write(BASE_CSS + "\n" + pygments_css())
    shutil.copyfile(os.path.join(OUT, "style.css"), os.path.join(ROOT, "style.css"))

    search_index = []
    cards = []
    total_words = total_refs = 0

    for f in files:
        slug = slug_of(f)
        raw = open(f).read()

        # Rewrite inter-guide .md links to .html
        raw = re.sub(r"\((\d\d-[a-z0-9-]+)\.md(#[^)]*)?\)", r"(\1.html\2)", raw)

        md = make_md()
        body = md.convert(raw)
        toc_tokens = md.toc_tokens

        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, flags=re.S)
        title = strip_tags(h1.group(1)).strip() if h1 else slug
        if h1:
            body = body[: h1.start()] + body[h1.end() :]

        words = len(re.findall(r"\w+", strip_tags(body)))
        mins = max(1, round(words / READING_WPM))
        refs = len(re.findall(r"^\d+\. \[", raw, flags=re.M))
        total_words += words
        total_refs += refs

        body = add_anchors(body)
        body = wrap_lede(body)

        # Excerpt for the index card
        lede = re.search(
            r'<div class="lede"><p><strong>Why this matters[^<]*</strong>(.*?)</p>',
            body,
            flags=re.S,
        )
        excerpt = strip_tags(lede.group(1)).strip() if lede else ""
        excerpt = re.sub(r"\s+", " ", excerpt)
        if len(excerpt) > 230:
            excerpt = excerpt[:227].rsplit(" ", 1)[0] + "..."

        for t in toc_tokens:
            search_index.append(
                {"g": slug, "t": title, "h": strip_tags(t["name"]), "a": t["id"]}
            )
            for c in t.get("children", []):
                search_index.append(
                    {"g": slug, "t": title, "h": strip_tags(c["name"]), "a": c["id"]}
                )

        num = slug.split("-")[0]
        pills = [f"Guide {num}"] + [
            f"{refs} refs",
            f"{words:,} words",
        ]
        inner = (
            sidebar_html(files, slug, "")
            + '<div class="main"><article class="doc">'
            + topbar(f"~{mins} min read", pills)
            + f"<h1>{title}</h1>"
            + body
            + f'<p class="disclaimer">{DISCLAIMER_HTML}</p>'
            + "</article>"
            + rail_html(toc_tokens)
            + "</div>"
        )
        html_doc = page(
            f"{title} - {LIBRARY_TITLE}",
            "",
            f'<div class="shell">{inner}</div><script>{PAGE_JS}</script>',
            "style.css",
        )
        with open(os.path.join(OUT, f"{slug}.html"), "w") as fh:
            fh.write(html_doc)

        label = META.get(slug, (title, []))[0]
        tags = META.get(slug, (title, []))[1]
        cards.append(
            f"""<a class="card" href="html/{slug}.html" data-search="{htmllib.escape((label + " " + " ".join(tags) + " " + excerpt).lower())}">
  <span class="cnum">{num}</span>
  <h3>{label}</h3>
  <p>{htmllib.escape(excerpt)}</p>
  <div class="tags">{"".join(f'<span class="tag">{t}</span>' for t in tags)}</div>
  <div class="card-foot">{mins} min &middot; {refs} refs</div>
</a>"""
        )

    build_index(files, cards, search_index, total_words, total_refs)
    print(f"Built {len(files)} guides -> {OUT}")
    print(f"  {total_words:,} words, {total_refs} numbered references")


def build_index(files, cards, search_index, total_words, total_refs):
    idx_json = json.dumps(search_index, separators=(",", ":"))
    hero = f"""<div class="hero">
  <h1>Temporal Cloud Infrastructure Ramp-Up</h1>
  <p class="tagline">{len(files)} deep guides I wrote to ramp up for a new infrastructure role:
  standing up, operating, and tearing down Temporal Cloud cells across AWS, GCP, and Azure,
  and the open-source tools underneath. Written for an engineer who knows distributed
  systems and needs the specific tools, their real models, and the places they bite.</p>
  <div class="stats">
    <div class="stat"><span class="n">{len(files)}</span><span class="l">guides</span></div>
    <div class="stat"><span class="n">{total_words // 1000}k</span><span class="l">words</span></div>
    <div class="stat"><span class="n">{total_refs}</span><span class="l">cited sources</span></div>
    <div class="stat"><span class="n">~{round(total_words / READING_WPM / 60)}h</span><span class="l">read time</span></div>
  </div>
  <div class="note">
    <p>{DISCLAIMER_HTML}</p>
  </div>
  <div class="note">
    <p><strong>Start with guide 12, the cell-lifecycle synthesis.</strong> It is the map:
    it explains what a cell is, the exact dependency order for bringing one up, the
    bootstrap paradoxes between these tools, and a 30/60/90 plan that points back into
    the rest. Its <em>How to use this library</em> section gives a suggested reading
    order. Everything else is a deep dive you can read in any order.</p>
    <p>Once you have read something, drill it: <a href="drill.html"><strong>215
    flashcards</strong></a> built from the production gotchas and hard numbers, with
    Leitner boxes, a timed self-test, and a 49-card <em>before day 1</em> set.</p>
  </div>
  <input class="searchbox" id="q" type="search"
         placeholder="Search every heading across all {len(files)} guides..." autocomplete="off">
  <div id="hits"></div>
  <div class="cards" id="cards">
    {"".join(cards)}
  </div>
  <div class="note">
    <p><strong>Sources.</strong> Every version number, default, limit, and deprecation
    date carries an inline link to a primary source, plus a numbered reference list at
    the end of each guide. Facts were verified against live documentation on
    2026-08-29 &mdash; this stack churns fast, so re-check anything load-bearing
    before you build on it. Claims that could not be verified are explicitly marked
    as such in the text.</p>
  </div>
</div>"""

    js = (
        """
const IDX = """
        + idx_json
        + r""";
const q=document.getElementById('q'), hits=document.getElementById('hits'),
      cards=document.getElementById('cards');
q.addEventListener('input',function(){
  const v=q.value.trim().toLowerCase();
  if(!v){hits.innerHTML=''; cards.style.display='';
    document.querySelectorAll('.card').forEach(c=>c.style.display=''); return;}
  cards.style.display='';
  document.querySelectorAll('.card').forEach(c=>{
    c.style.display=(c.dataset.search||'').includes(v)?'':'none';
  });
  const m=IDX.filter(r=>r.h.toLowerCase().includes(v)).slice(0,25);
  hits.innerHTML=m.length
    ? '<div style="margin-bottom:1.2rem">'+m.map(r=>
        '<div class="hit"><a href="html/'+r.g+'.html#'+r.a+'">'+r.h+
        '</a><span class="where">'+r.t+'</span></div>').join('')+'</div>'
    : '<div class="hit" style="color:var(--fg-dim)">No heading matches.</div>';
});
"""
    )

    body = (
        sidebar_html(files, None, "html/")
        + '<div class="main"><div class="doc">'
        + topbar("library index", ["updated 2026-08-29"])
        + hero
        + "</div></div>"
    )
    with open(os.path.join(ROOT, "index.html"), "w") as fh:
        fh.write(
            page(
                LIBRARY_TITLE,
                "",
                f'<div class="shell">{body}</div><script>{js}</script>',
                "style.css",
            )
        )


if __name__ == "__main__":
    build()

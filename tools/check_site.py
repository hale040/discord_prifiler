#!/usr/bin/env python3
"""L2/L3/L5: целостность HTML, баланс CSS, контентные константы."""
import sys, re, pathlib
from html.parser import HTMLParser

VOID = {"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}

class Balance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.stack=[]; self.errors=[]
    def handle_starttag(self, tag, attrs):
        if tag not in VOID: self.stack.append((tag, self.getpos()))
    def handle_endtag(self, tag):
        if tag in VOID: return
        if not self.stack: self.errors.append(f"лишний </{tag}> на {self.getpos()}"); return
        if self.stack[-1][0]==tag: self.stack.pop()
        else:
            for i in range(len(self.stack)-1,-1,-1):
                if self.stack[i][0]==tag:
                    for t,pos in self.stack[i+1:]: self.errors.append(f"не закрыт <{t}> (открыт на {pos})")
                    del self.stack[i:]; break
            else: self.errors.append(f"несоответствующий </{tag}> на {self.getpos()}")

def main(root="."):
    R=pathlib.Path(root); fails=[]; warns=[]
    idx=R/"index.html"
    if not idx.exists(): fails.append("нет index.html"); print_report(fails,warns); return 1
    src=idx.read_text(encoding="utf-8",errors="replace")
    p=Balance(); p.feed(src); p.close()
    for t,pos in p.stack: p.errors.append(f"не закрыт <{t}> (открыт на {pos})")
    if p.errors: fails += ["L2 HTML: "+e for e in p.errors[:20]]
    else: print("L2 HTML: теги сбалансированы, errors=0")
    # локальные ссылки/ресурсы
    refs=re.findall(r'(?:href|src)="([^"#:][^"]*)"',src)
    missing=[r for r in refs if not r.startswith(("http","//","mailto:","tel:","data:","#")) and not (R/r.split("?")[0]).exists()]
    if missing: fails += [f"L2 битая локальная ссылка: {m}" for m in sorted(set(missing))]
    else: print(f"L2 ссылки: проверено {len(refs)} локальных, битых 0")
    css=(R/"styles.css").read_text(encoding="utf-8",errors="replace")
    bal=css.count("{")-css.count("}")
    if bal: fails.append(f"L3 styles.css: разбаланс скобок {bal:+d}")
    else: print(f"L3 CSS: {{}} сбалансированы ({css.count('{')} шт)")
    # L5 контент
    tel=set(re.findall(r"\+7[\s\-\(0-9]{9,}",src)); sm=R/"sitemap.xml"
    if sm.exists():
        locs=re.findall(r"<loc>(.*?)</loc>",sm.read_text(encoding="utf-8"))
        bad=[l for l in locs if not l.startswith("https://")]
        (warns if bad else []) .append("L5 sitemap: https нарушен: "+", ".join(bad)) if bad else print(f"L5 sitemap: {len(locs)} URL, все https")
    print(f"L5 телефон: найдено форматов +7...: {len(tel)}")
    print_report(fails,warns); return 1 if fails else 0

def print_report(fails,warns):
    for w in warns: print("WARN:",w)
    for f in fails: print("FAIL:",f)
    print("ИТОГ:", "FAIL" if fails else "PASS")

if __name__=="__main__": sys.exit(main(*sys.argv[1:]))

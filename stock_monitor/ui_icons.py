"""Small code-drawn desktop icons; no network, external artwork or market data."""
import math
from PIL import Image, ImageDraw, ImageTk


def photo(root, name, size=18, color=None):
    if color is None:
        from tkinter import ttk
        color = ttk.Style(root).lookup('Desk.TLabel','foreground') or '#d8e5f4'
    cache = getattr(root, '_desk_icons', None)
    if cache is None:root._desk_icons = cache = {}
    key = (name, size, color)
    if key in cache:return cache[key]
    image = Image.new('RGBA', (96, 96))
    d = ImageDraw.Draw(image)
    def line(points, fill=color, width=5):d.line([(x*4,y*4) for x,y in points], fill=fill, width=width, joint='curve')
    def box(bounds, fill=None, outline=color, width=5, radius=0):
        coords=tuple(v*4 for v in bounds)
        if radius:d.rounded_rectangle(coords,radius=radius*4,fill=fill,outline=outline,width=width)
        else:d.rectangle(coords,fill=fill,outline=outline,width=width)
    if name == 'menu':
        for y in (5,12,19):line([(3,y),(21,y)])
    elif name in ('globe','eu'):
        d.ellipse((4,4,92,92), fill='#064f94', outline='#1d9ee7', width=4)
        if name == 'globe':
            d.ellipse((26,4,70,92),outline='#a5e9ff',width=3)
            for y in (30,48,66):d.line((8,y,88,y), fill='#a5e9ff',width=3)
        else:
            for a in range(12):
                x=48+27*math.sin(a*math.pi/6);y=48+27*math.cos(a*math.pi/6)
                d.ellipse((x-3,y-3,x+3,y+3),fill='#ffdb28')
    elif name in ('us','de'):
        if name=='us':
            for i in range(13):d.rectangle((4,4+i*88/13,92,4+(i+1)*88/13),fill='#e44754' if i%2==0 else '#ffffff')
            d.rectangle((4,4,46,50),fill='#194790')
            for y in (12,24,36):
                for x in (12,24,36):d.ellipse((x-2,y-2,x+2,y+2),fill='white')
        else:
            for i,c in enumerate(('#15181c','#e43640','#ffcf32')):d.rectangle((4,4+i*88/3,92,4+(i+1)*88/3),fill=c)
        mask=Image.new('L',(96,96));ImageDraw.Draw(mask).ellipse((4,4,92,92),fill=255);image.putalpha(mask)
    elif name in ('star','star_filled'):
        points=[]
        for i in range(10):
            a=-math.pi/2+i*math.pi/5;r=10 if i%2==0 else 4.5
            points.append((48+4*r*math.cos(a),48+4*r*math.sin(a)))
        if name=='star_filled':d.polygon(points,fill='#ffd020')
        d.line(points+[points[0]],fill='#ffd020',width=5,joint='curve')
    elif name in ('chart','line'):
        line([(3,3),(3,21),(22,21)])
        line([(5,16),(10,11),(14,14),(21,5)])
    elif name=='candles':
        for x,a,b,c,e in [(6,3,21,8,16),(12,2,19,5,12),(18,7,22,11,18)]:
            line([(x,a),(x,b)]);box((x-2,c,x+2,e),fill='#0bbced' if x==12 else color,outline=color,width=2)
    elif name=='camera':
        box((2,6,22,21),radius=2);box((7,3,15,6),radius=1)
        d.ellipse((32,36,64,68),outline=color,width=5)
    elif name=='gear':
        d.ellipse((23,23,73,73),outline=color,width=5);d.ellipse((37,37,59,59),outline=color,width=5)
        for i in range(8):
            a=i*math.pi/4;line([(12+6*math.cos(a),12+6*math.sin(a)),(12+10*math.cos(a),12+10*math.sin(a))],width=9)
    elif name in ('draw','ruler'):
        line([(4,20),(20,4)])
        if name=='draw':
            for x,y in ((4,20),(20,4)):d.ellipse((x*4-6,y*4-6,x*4+6,y*4+6),outline=color,width=4)
        else:
            for p in (7,11,15):line([(p,24-p),(p-2,22-p)],width=4)
    elif name=='template':
        for x in (3,13):
            for y in (3,13):box((x,y,x+8,y+8),radius=1,width=4)
    elif name=='fullscreen':
        for points in ([(3,9),(3,3),(9,3)],[(15,3),(21,3),(21,9)],[(21,15),(21,21),(15,21)],[(9,21),(3,21),(3,15)]):line(points)
    elif name=='indicators':
        for x,y in ((6,8),(12,16),(18,5)):
            line([(x,2),(x,22)]);d.ellipse((x*4-5,y*4-5,x*4+5,y*4+5),fill='#0d1827',outline=color,width=4)
    elif name.startswith('drawing_'):
        kind=name.removeprefix('drawing_')
        if kind=='hline':line([(3,12),(21,12)])
        elif kind=='vline':line([(12,3),(12,21)])
        elif kind in ('trend','ray'):
            line([(3,21),(21,3)])
            if kind=='ray':line([(14,3),(21,3),(21,10)])
            else:
                for x,y in ((4,20),(20,4)):d.ellipse((x*4-5,y*4-5,x*4+5,y*4+5),fill=color)
        elif kind=='zone':box((3,5,21,19),width=4)
        elif kind in ('ellipse','circle'):d.ellipse((12,20 if kind=='ellipse' else 12,84,76 if kind=='ellipse' else 84),outline=color,width=5)
        elif kind=='triangle':line([(3,21),(12,3),(21,21),(3,21)])
        elif kind in ('fib','fib_extension'):
            for y in (3,7,12,17,21):line([(3,y),(21,y)],width=3)
            if kind=='fib_extension':line([(3,20),(9,10),(14,14),(21,3)],width=4)
        elif kind=='fib_fan':
            for y in (3,7,12,17,21):line([(3,21),(21,y)],width=3)
        elif kind=='fib_arcs':
            for r in (6,12,18):d.arc(((12-r)*4,(20-r)*4,(12+r)*4,(20+r)*4),180,360,fill=color,width=3)
        elif kind=='fib_timezones':
            for x in (2,4,7,12,20):line([(x,3),(x,21)],width=3)
        elif kind=='regression':
            for offset in (0,5,10):line([(3,12+offset),(21,2+offset)],width=3)
        elif kind=='brush':
            line([(2,18),(6,5),(11,19),(16,6),(22,11)],width=4)
        elif kind in ('polygon','polyline'):
            points=[(3,18),(7,4),(17,6),(22,18),(12,13)]
            line(points+([points[0]] if kind=='polygon' else []),width=4)
        elif kind=='channel':
            line([(3,15),(21,3)]);line([(3,21),(21,9)])
        elif kind=='extended':line([(2,22),(22,2)])
        elif kind in ('arrow','double_arrow'):
            line([(3,21),(21,3),(21,10)]);line([(21,3),(14,3)])
            if kind=='double_arrow':line([(3,14),(3,21),(10,21)])
        elif kind in ('text','note'):
            line([(5,4),(19,4)]);line([(12,4),(12,21)])
            if kind=='note':box((2,2,22,22),width=2,radius=2)
        elif kind.startswith('gann_'):
            if kind=='gann_fan':
                for x,y in ((7,3),(13,3),(21,3),(21,11),(21,17)):line([(3,21),(x,y)],width=3)
            else:
                box((3,3,21,21),width=3)
                for n in (9,15):line([(3,n),(21,n)],width=2);line([(n,3),(n,21)],width=2)
                if kind=='gann_square':line([(3,21),(21,3)],width=3);line([(3,3),(21,21)],width=3)
        elif kind in ('abcd','xabcd','elliott_impulse','elliott_correction','elliott_triangle'):
            line([(2,20),(6,9),(10,16),(14,5),(18,12),(22,2)],width=4)
            for x,y in ((2,20),(10,16),(18,12)):d.ellipse((x*4-3,y*4-3,x*4+3,y*4+3),fill=color)
        elif kind=='cycles':
            for x in (3,9,15,21):line([(x,3),(x,21)],width=3)
        elif kind=='measure':
            line([(3,21),(21,3)])
            for p in (7,11,15):line([(p,24-p),(p-2,22-p)],width=4)
        elif kind=='marker':
            d.line([(12*4,3*4),(21*4,12*4),(12*4,21*4),(3*4,12*4),(12*4,3*4)],fill=color,width=5)
    elif name=='reset':
        d.arc((12,12,84,84),start=35,end=310,fill=color,width=5);line([(19,3),(20,10),(13,8)])
    cache[key]=ImageTk.PhotoImage(image.resize((size,size),Image.Resampling.LANCZOS),master=root)
    return cache[key]


def bind(widget, root, name, size=18):
    widget.configure(image=photo(root,name,size))
    if not hasattr(root,'_desk_icon_widgets'):root._desk_icon_widgets=[]
    root._desk_icon_widgets.append((widget,name,size))

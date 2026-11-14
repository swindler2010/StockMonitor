"""Compact rounded ttk buttons; keep native commands, focus and keyboard bindings."""
from tkinter import ttk
from PIL import Image, ImageDraw, ImageTk


def install(root, palette):
    style=ttk.Style(root)
    # Nine-slice images scale to the label without stretching the corners.
    images=[]
    for name,fill,outline in (
        ('normal',palette['panel'],palette['grid']),
        ('active',palette['grid'],palette['accent']),
        ('pressed',palette['panel'],palette['accent']),
        ('focus',palette['panel'],palette['accent']),
        ('disabled',palette['bg'],palette['grid'])):
        image=Image.new('RGBA',(96,96))
        draw=ImageDraw.Draw(image)
        draw.rounded_rectangle((2,2,93,93),radius=24,fill=fill,outline=outline,width=3)
        photo=ImageTk.PhotoImage(image.resize((24,24),Image.Resampling.LANCZOS),master=root)
        images.append((name,photo))
    root._rounded_button_images=images
    element='SMRounded'+str(getattr(root,'_rounded_button_generation',0))
    root._rounded_button_generation=getattr(root,'_rounded_button_generation',0)+1
    pictures=dict(images)
    style.element_create(element,'image',pictures['normal'],
        ('disabled',pictures['disabled']),('pressed',pictures['pressed']),
        ('active',pictures['active']),('focus',pictures['focus']),border=5,sticky='nsew')
    style.layout('TButton',[(element,{'sticky':'nsew','children':[
        ('Button.padding',{'sticky':'nsew','children':[('Button.label',{'sticky':'nsew'})]})]})])
    style.configure('TButton',padding=(6,1),width=0,borderwidth=0,
                    foreground=palette['text'],font=('Segoe UI',9),anchor='center')
    style.map('TButton',foreground=[('disabled',palette['muted']),('!disabled',palette['text'])])
    style.configure('Accent.TButton',padding=(6,1),width=0,font=('Segoe UI',9))
    style.configure('Treeview',rowheight=30)


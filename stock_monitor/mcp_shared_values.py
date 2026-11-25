"""Lossless shared-object references for readonly snapshot delivery only."""

def pointer(parent,key):
    return parent+'/'+str(key).replace('~','~0').replace('/','~1')

class ReferenceTree(dict):
    def __init__(self,value,references):
        super().__init__(value);self.references=references

    def resolve(self,path):
        return self._resolve(path,set())[0]

    def _resolve(self,path,seen):
        node=self;current=''
        parts=path.split('/')[1:] if path else []
        for n in range(len(parts)+1):
            while current in self.references:
                if current in seen:raise ValueError('PAGE_REFERENCE_CYCLE')
                seen=seen|{current}
                node,current=self._resolve(self.references[current],seen)
            if n==len(parts):return node,current
            part=parts[n];key=part.replace('~1','/').replace('~0','~')
            node=node[int(key)] if isinstance(node,list) else dict.__getitem__(node,key)
            current+='/'+part

    def get(self,key,default=None):
        return self.resolve(pointer('',key)) if key in self else default


def compact(value):
    """Only real container aliases become references, never user marker fields.

    Original pointer paths resolve automatically. Nested inline markers name
    the same pinned value explicitly; no analysis validity is extended.
    """
    if not isinstance(value,dict):return value,{}
    seen={};active=set();references={}
    def visit(node,path):
        if not isinstance(node,(dict,list)):return node
        identity=id(node)
        if identity in active:raise ValueError('PAGE_CIRCULAR_VALUE')
        children=node.values() if isinstance(node,dict) else node
        shared=bool(node) and (len(node)>3 or any((isinstance(v,(dict,list)) and bool(v)) or (isinstance(v,str) and len(v)>256) for v in children))
        if shared and identity in seen:
            references[path]=seen[identity]
            return dict(_read_reference=seen[identity])
        if shared:seen[identity]=path
        active.add(identity)
        if isinstance(node,dict):result={k:visit(v,pointer(path,k)) for k,v in node.items()}
        else:result=[visit(v,pointer(path,n)) for n,v in enumerate(node)]
        active.remove(identity)
        return result
    return visit(value,''),references

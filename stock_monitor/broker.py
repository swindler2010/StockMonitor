"""Official Scalable CLI read boundary; never invokes login or a write command."""
import json
from pathlib import Path
import subprocess
import os
import re

class ScalableReadOnly:
    def __init__(self,executable):
        path=Path(executable)
        if not path.is_file():raise ValueError('Выберите установленный официальный исполняемый файл Scalable CLI.')
        self.executable=str(path.resolve())

    def read(self,operation,isin=None):
        allowed={'overview':['broker','overview'],'holdings':['broker','holdings'],
                 'capabilities':['capabilities'],'news':['broker','security-news']}
        if operation not in allowed:raise ValueError('Разрешены только чтение обзора, позиций, возможностей и новостей.')
        args=[self.executable]+allowed[operation]
        if operation=='news':
            if not isin or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',isin):raise ValueError('Некорректный ISIN')
            args+=['--isin',isin,'--locale','en_DE']
        args+=['--json']
        result=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',timeout=45,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:raise RuntimeError('Scalable CLI: чтение не выполнено. Проверьте вход и доступ в официальном CLI.')
        return json.loads(result.stdout)


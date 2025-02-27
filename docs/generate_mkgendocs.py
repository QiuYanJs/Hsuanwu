import os
import sys

curren_dir_path = os.path.dirname(os.path.realpath(__file__))
parent_dir_path = os.path.abspath(os.path.join(curren_dir_path, os.pardir))
sys.path.append(parent_dir_path)

import inspect
import ruamel.yaml

from hsuanwu import common
from hsuanwu import env
from hsuanwu import evaluation
from hsuanwu.common import engine
from hsuanwu.xploit import encoder, agent, storage
from hsuanwu.xplore import distribution, reward, augmentation

pages = {
    'sources_dir': 'docs/api_docs',
    'templates_dir': None,
    'repo': 'https://github.com/RLE-Foundation/Hsuanwu',
    'version': 'main',
    'pages': []
}

for module in [engine, common, encoder, agent, storage, 
           reward, augmentation, distribution, env, evaluation]:
    last_file = None
    save_old = False
    for name, item in inspect.getmembers(module):
        if inspect.isclass(item):
            file = inspect.getfile(item).split('hsuanwu')[1]
            file = file.lstrip(file[0])
            page = {
                'page': file.replace('.py', '.md'),
                'source': 'hsuanwu/' + file,
                'classes': [item.__name__]
            }
            pages['pages'].append(page)

        if inspect.isfunction(item):
            file = inspect.getfile(item).split('hsuanwu')[1]
            file = file.lstrip(file[0])
            if file == last_file:
                page['functions'].append(item.__name__)
                save_old = False
            else:
                save_old = True
                page = {
                    'page': file.replace('.py', '.md'),
                    'source': 'hsuanwu/' + file,
                    'functions': [item.__name__]
                }

            if save_old:
                pages['pages'].append(page)
            last_file = file

yaml = ruamel.yaml.YAML()
yaml.indent(sequence=4, offset=2)

with open('docs/mkgendocs.yml', 'w') as f:
    yaml.dump(pages, f)
    f.close()
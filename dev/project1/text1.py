table = [o for o in op('/').findChildren() if 'Embody/externalizer' in o.path]
print(table)
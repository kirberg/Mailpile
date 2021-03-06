#!/usr/bin/python
#coding:utf-8


from mailpile.plugins.contacts import ContactImporter, register_contact_importer
from mailpile.vcard import CardDAV
import sys
import re
import getopt

from sys import stdin, stdout, stderr


def hexcmp(x, y):
    try:
        a = int(x, 16)
        b = int(y, 16)
        if a < b:  return -1
        if a > b:  return 1
        return 0

    except:
        return cmp(x, y)


class MorkImporter(ContactImporter):
    # Based on Demork by Mike Hoye <mhoye@off.net>
    # Which is based on Mindy by Kumaran Santhanam <kumaran@alumni.stanford.org>
    #
    # To understand the insanity that is Mork, read these:
    #  http://www-archive.mozilla.org/mailnews/arch/mork/primer.txt
    #  http://www.jwz.org/blog/2004/03/when-the-database-worms-eat-into-your-brain/
    #

    required_parameters = ["filename"]
    optional_parameters = ["data"]
    short_name = "mork"
    format_name = "Mork Database"
    format_description = "Thunderbird contacts database format."

    class Database:
        def __init__ (self):
            self.cdict = {}
            self.adict = {}
            self.tables = {}

    class Table:
        def __init__ (self):
            self.id = None
            self.scope = None
            self.kind = None
            self.rows = {}

    class Row:
        def __init__ (self):
            self.id = None
            self.scope = None
            self.cells = []

    class Cell:
        def __init__ (self):
            self.column = None
            self.atom = None


    def escapeData(self, match):
        return match.group() \
                   .replace('\\\\n', '$0A') \
                   .replace('\\)', '$29') \
                   .replace('>', '$3E') \
                   .replace('}', '$7D') \
                   .replace(']', '$5D')

    pCellText   = re.compile(r'\^(.+?)=(.*)')
    pCellOid    = re.compile(r'\^(.+?)\^(.+)')
    pCellEscape = re.compile(r'((?:\\[\$\0abtnvfr])|(?:\$..))')
    pMindyEscape = re.compile('([\x00-\x1f\x80-\xff\\\\])')

    def escapeMindy(self, match):
        s = match.group()
        if s == '\\': return '\\\\'
        if s == '\0': return '\\0'
        if s == '\r': return '\\r'
        if s == '\n': return '\\n'
        return "\\x%02x" % ord(s)

    def encodeMindyValue(self, value):
        return pMindyEscape.sub(self.escapeMindy, value)


    backslash = { '\\\\' : '\\',
                  '\\$'  : '$',
                  '\\0'  : chr(0),
                  '\\a'  : chr(7),
                  '\\b'  : chr(8),
                  '\\t'  : chr(9),
                  '\\n'  : chr(10),
                  '\\v'  : chr(11),
                  '\\f'  : chr(12),
                  '\\r'  : chr(13) }

    def unescapeMork (match):
        s = match.group()
        if s[0] == '\\':
            return backslash[s]
        else:
            return chr(int(s[1:], 16))

    def decodeMorkValue (value):
        global pCellEscape
        return pCellEscape.sub(unescapeMork, value)

    def addToDict (dict, cells):
        for cell in cells:
            eq  = cell.find('=')
            key = cell[1:eq]
            val = cell[eq+1:-1]
            dict[key] = decodeMorkValue(val)

    def getRowIdScope (rowid, cdict):
        idx = rowid.find(':')
        if idx > 0:
            return (rowid[:idx], cdict[rowid[idx+2:]])
        else:
            return (rowid, None)
            
    def delRow (db, table, rowid):
        (rowid, scope) = getRowIdScope(rowid, db.cdict)
        if scope:
            rowkey = rowid + "/" + scope
        else:
            rowkey = rowid + "/" + table.scope

        if rowkey in table.rows:
            del table.rows[rowkey]

    def addRow (db, table, rowid, cells):
        global pCellText
        global pCellOid

        row = Row()
        row.id, row.scope = getRowIdScope(rowid, db.cdict)

        for cell in cells:
            obj = Cell()
            cell = cell[1:-1]

            match = pCellText.match(cell)
            if match:
                obj.column = db.cdict[match.group(1)]
                obj.atom   = decodeMorkValue(match.group(2))

            else:
                match = pCellOid.match(cell)
                if match:
                    obj.column = db.cdict[match.group(1)]
                    obj.atom   = db.adict[match.group(2)]

            if obj.column and obj.atom:
                row.cells.append(obj)

        if row.scope:
            rowkey = row.id + "/" + row.scope
        else:
            rowkey = row.id + "/" + table.scope

        if rowkey in table.rows:
            print >>stderr, "ERROR: duplicate rowid/scope %s" % rowkey
            print >>stderr, cells

        table.rows[rowkey] = row
        
    def inputMork(self, data):
        # Remove beginning comment
        pComment = re.compile('//.*')
        data = pComment.sub('', data, 1)

        # Remove line continuation backslashes
        pContinue = re.compile(r'(\\(?:\r|\n))')
        data = pContinue.sub('', data)

        # Remove line termination
        pLine = re.compile(r'(\n\s*)|(\r\s*)|(\r\n\s*)')
        data = pLine.sub('', data)

        # Create a database object
        db          = Database()

        # Compile the appropriate regular expressions
        pCell       = re.compile(r'(\(.+?\))')
        pSpace      = re.compile(r'\s+')
        pColumnDict = re.compile(r'<\s*<\(a=c\)>\s*(?:\/\/)?\s*(\(.+?\))\s*>')
        pAtomDict   = re.compile(r'<\s*(\(.+?\))\s*>')
        pTable      = re.compile(r'\{-?(\d+):\^(..)\s*\{\(k\^(..):c\)\(s=9u?\)\s*(.*?)\}\s*(.+?)\}')
        pRow        = re.compile(r'(-?)\s*\[(.+?)((\(.+?\)\s*)*)\]')

        pTranBegin  = re.compile(r'@\$\$\{.+?\{\@')
        pTranEnd    = re.compile(r'@\$\$\}.+?\}\@')

        # Escape all '%)>}]' characters within () cells
        data = pCell.sub(self.escapeData, data)

        # Iterate through the data
        index  = 0
        length = len(data)
        match  = None
        tran   = 0
        while True:
            if match:  index += match.span()[1]
            if index >= length:  break
            sub = data[index:]

            # Skip whitespace
            match = pSpace.match(sub)
            if match:
                index += match.span()[1]
                continue

            # Parse a column dictionary
            match = pColumnDict.match(sub)
            if match:
                m = pCell.findall(match.group())
                # Remove extraneous '(f=iso-8859-1)'
                if len(m) >= 2 and m[1].find('(f=') == 0:
                    m = m[1:]
                addToDict(db.cdict, m[1:])
                continue

            # Parse an atom dictionary
            match = pAtomDict.match(sub)
            if match:
                cells = pCell.findall(match.group())
                addToDict(db.adict, cells)
                continue

            # Parse a table
            match = pTable.match(sub)
            if match:
                id = match.group(1) + ':' + match.group(2)

                try:
                    table = db.tables[id]

                except KeyError:
                    table = Table()
                    table.id    = match.group(1)
                    table.scope = db.cdict[match.group(2)]
                    table.kind  = db.cdict[match.group(3)]
                    db.tables[id] = table

                rows = pRow.findall(match.group())
                for row in rows:
                    cells = pCell.findall(row[2])
                    rowid = row[1]
                    if tran and rowid[0] == '-':
                        rowid = rowid[1:]
                        delRow(db, db.tables[id], rowid)

                    if tran and row[0] == '-':
                        pass

                    else:
                        addRow(db, db.tables[id], rowid, cells)
                continue

            # Transaction support
            match = pTranBegin.match(sub)
            if match:
                tran = 1
                continue

            match = pTranEnd.match(sub)
            if match:
                tran = 0
                continue

            match = pRow.match(sub)
            if match and tran:
                print >>stderr, "WARNING: using table '1:^80' for dangling row: %s" % match.group()
                rowid = match.group(2)
                if rowid[0] == '-':
                    rowid = rowid[1:]

                cells = pCell.findall(match.group(3))
                delRow(db, db.tables['1:80'], rowid)
                if row[0] != '-':
                    addRow(db, db.tables['1:80'], rowid, cells)
                continue

            # Syntax error
            print >>stderr, "ERROR: syntax error while parsing MORK file"
            print >>stderr, "context[%d]: %s" % (index, sub[:40])
            index += 1

        # Return the database
        self.db = db
        return db

    def morkToHash(self):
        results = []
        columns = self.db.cdict.keys()
        columns.sort(hexcmp)

        tables = self.db.tables.keys()
        tables.sort(hexcmp)

        for table in [ self.db.tables[k] for k in tables ]:
            rows = table.rows.keys()
            rows.sort(hexcmp)
            for row in [ table.rows[k] for k in rows ]:
                email = name = ""
                result = {}
                for cell in row.cells:
                    result[cell.column] = cell.atom
                    if cell.column == "PrimaryEmail":
                        result["email"] = self.encodeMindyValue(cell.atom)
                    elif cell.column == "DisplayName":
                        result["name"] = self.encodeMindyValue(cell.atom)
                    # print "n/e: %s:%s" % (result["email"], result["name"])
                results.append(result)

        return results

    def load(self):
        if self.args["filename"] == "-":
            data = self.args.get("data", "")
        else:
            fh = open(filename, "rt")
            data = fh.read()

        if data.find("<mdb:mork") < 0:
            raise ValueError("Mork file required")

        self.inputMork(data)

    def get_contacts(self):
        return self.morkToHash()

    def filter_contacts(self, terms):
        mh = self.morkToHash()
        return filter(lambda x: any([v.find(terms) >= 0 for v in x.values()]) , f)


class CardDAVImporter(ContactImporter):
    required_parameters = ["host", "url"]
    optional_parameters = ["port", "username", "password", "protocol"]
    short_name = "carddav"
    format_name = "CardDAV Server"
    format_description = "CardDAV HTTP contact server."

    def load(self):
        host = self.args.get("host")
        url = self.args.get("url")
        port = self.args.get("port", None)
        username = self.args.get("username", None)
        password = self.args.get("password", None)
        protocol = self.args.get("protocol", "https")
        self.carddav = CardDAV(host, url, port, username, password, protocol)

    def get_contacts(self):
        results = []
        cards = self.carddav.list_vcards()
        for card in cards:
            results.append(self.carddav.get_vcard(card))

        return results

    def filter_contacts(self, terms):
        pass




register_contact_importer(MorkImporter)
register_contact_importer(CardDAVImporter)


if __name__ == "__main__":
    import json
    filename = sys.argv[1]

    m = MorkImporter(filename=filename)
    m.load()
    print json.dumps(m.get_contacts(data))

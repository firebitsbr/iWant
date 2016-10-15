class MainException(Exception):
    def __init__(self, code):
        self.code = code
        self.msg = {
                1: 'shared folder doesn\'t exist',
                2: 'corrupted .iwant.conf file'
        }

    def __str__(self):
        return 'Error [{0}] => {1}'.format(self.code, self.msg[self.code])